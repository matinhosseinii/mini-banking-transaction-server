"""Reusable synchronous HTTP protocol client with safe mutation retries."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

import httpx

from client.config import RetryPolicy, Timeouts
from client.pending import PendingRequest


class ResultKind(StrEnum):
    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    CLIENT_ERROR = "CLIENT_ERROR"
    TECHNICAL_ERROR = "TECHNICAL_ERROR"


@dataclass(frozen=True, slots=True)
class ClientResult:
    kind: ResultKind
    status_code: int | None
    data: dict[str, Any] | None
    detail: str

    @property
    def definitive(self) -> bool:
        return self.kind is not ResultKind.UNKNOWN


ProgressCallback = Callable[[str], None]


class BankingHttpClient:
    """HTTP transport independent of argparse and terminal formatting."""

    def __init__(
        self,
        base_url: str,
        timeouts: Timeouts,
        retry: RetryPolicy,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.retry = retry
        self.clock = clock
        self.sleep = sleep
        self.jitter = jitter
        self.client = httpx.Client(
            base_url=base_url,
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=timeouts.connect,
                read=timeouts.read,
                write=timeouts.write,
                pool=timeouts.pool,
            ),
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def read(self, path: str, *, params: dict[str, Any] | None = None) -> ClientResult:
        """Perform one read-only request without automatic retries."""
        try:
            response = self.client.get(path, params=params)
        except httpx.RequestError:
            return ClientResult(
                ResultKind.TECHNICAL_ERROR, None, None, "Service request failed."
            )
        if 200 <= response.status_code < 300:
            return ClientResult(
                ResultKind.SUCCESS,
                response.status_code,
                _json_object(response),
                "Success.",
            )
        return _interpret_response(response, lookup=True)

    def mutate(
        self, pending: PendingRequest, progress: ProgressCallback
    ) -> ClientResult:
        """Send one persisted mutation with bounded transient retries."""
        started = self.clock()
        for attempt in range(1, self.retry.max_attempts + 1):
            if attempt > 1:
                delay = self.retry.backoff_seconds[attempt - 2] + self.jitter(
                    0, self.retry.jitter_seconds
                )
                remaining = self.retry.deadline_seconds - (self.clock() - started)
                if delay > remaining:
                    break
                progress(
                    f"Retrying request {pending.request_id} "
                    f"(attempt {attempt}/{self.retry.max_attempts}) in {delay:.1f}s"
                )
                self.sleep(delay)
            if self.clock() - started >= self.retry.deadline_seconds:
                break
            try:
                response = self.client.request(
                    pending.method,
                    pending.path,
                    json=pending.payload,
                    headers={"Idempotency-Key": str(pending.request_id)},
                )
            except httpx.RequestError:
                continue
            if response.status_code in {502, 503, 504}:
                continue
            if 300 <= response.status_code < 400:
                return self.lookup_pending(pending)
            if 500 <= response.status_code < 600:
                return self.lookup_pending(pending)
            if 200 <= response.status_code < 300:
                result = _validated_outcome(response, pending, require_replayed=True)
                return result if result is not None else self.lookup_pending(pending)
            return _interpret_response(response)
        return self.lookup_pending(pending)

    def lookup_pending(self, pending: PendingRequest) -> ClientResult:
        """Resolve one uncertain mutation with an independent lookup timeout."""
        try:
            response = self.client.get(
                f"/requests/{pending.request_id}",
                timeout=self.retry.lookup_timeout_seconds,
            )
        except httpx.RequestError:
            return ClientResult(
                ResultKind.UNKNOWN,
                None,
                None,
                "Mutation outcome remains unknown.",
            )
        if response.status_code == 404:
            return ClientResult(
                ResultKind.UNKNOWN,
                404,
                _json_object(response),
                "Mutation outcome remains unknown.",
            )
        if response.status_code != 200:
            return ClientResult(
                ResultKind.UNKNOWN,
                response.status_code,
                _json_object(response),
                "Mutation outcome remains unknown.",
            )
        result = _validated_outcome(response, pending, require_replayed=False)
        if result is not None:
            return result
        return ClientResult(
            ResultKind.UNKNOWN,
            response.status_code,
            _json_object(response),
            "Mutation outcome remains unknown.",
        )


_BUSINESS_ERRORS = {
    "ACCOUNT_NOT_FOUND",
    "ACCOUNT_ALREADY_CLOSED",
    "INSUFFICIENT_FUNDS",
    "NON_ZERO_BALANCE",
}


def _interpret_response(
    response: httpx.Response, *, lookup: bool = False
) -> ClientResult:
    data = _json_object(response)
    if 200 <= response.status_code < 300:
        if data.get("status") == "REJECTED":
            return ClientResult(
                ResultKind.REJECTED, response.status_code, data, "Rejected."
            )
        return ClientResult(ResultKind.SUCCESS, response.status_code, data, "Success.")
    error_code = data.get("error_code")
    detail = (
        data.get("detail") if isinstance(data.get("detail"), str) else "Request failed."
    )
    if error_code in _BUSINESS_ERRORS and not lookup:
        return ClientResult(ResultKind.REJECTED, response.status_code, data, detail)
    if 400 <= response.status_code < 500:
        return ClientResult(ResultKind.CLIENT_ERROR, response.status_code, data, detail)
    return ClientResult(ResultKind.TECHNICAL_ERROR, response.status_code, data, detail)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _validated_outcome(
    response: httpx.Response,
    pending: PendingRequest,
    *,
    require_replayed: bool,
) -> ClientResult | None:
    data = _json_object(response)
    required = {
        "request_id",
        "operation",
        "requested_account_id",
        "account_id",
        "amount",
        "status",
        "balance_before",
        "balance_after",
        "error_code",
        "created_at",
    }
    if not required.issubset(data):
        return None
    try:
        response_id = UUID(data["request_id"])
    except (TypeError, ValueError, AttributeError):
        return None
    if response_id != pending.request_id or data["operation"] != pending.operation:
        return None
    status = data["status"]
    if status not in {"SUCCEEDED", "REJECTED"}:
        return None
    for field in (
        "requested_account_id",
        "account_id",
        "amount",
        "balance_before",
        "balance_after",
    ):
        if not _nullable_integer(data[field]):
            return None
    if any(
        data[field] is not None and data[field] < 0
        for field in ("balance_before", "balance_after")
    ):
        return None
    if not isinstance(data["created_at"], str):
        return None
    try:
        datetime.fromisoformat(data["created_at"])
    except ValueError:
        return None
    if data["error_code"] is not None and not isinstance(data["error_code"], str):
        return None
    if require_replayed and not isinstance(data.get("replayed"), bool):
        return None
    if not _matches_original_arguments(data, pending):
        return None
    if status == "SUCCEEDED":
        if (
            data["error_code"] is not None
            or not _positive_integer(data["account_id"])
            or data["balance_after"] is None
        ):
            return None
        kind = ResultKind.SUCCESS
    else:
        if not isinstance(data["error_code"], str) or not data["error_code"]:
            return None
        kind = ResultKind.REJECTED
    return ClientResult(
        kind,
        response.status_code,
        data,
        "Success." if kind is ResultKind.SUCCESS else "Rejected.",
    )


def _matches_original_arguments(data: dict[str, Any], pending: PendingRequest) -> bool:
    if pending.operation == "CREATE_ACCOUNT":
        return data["requested_account_id"] is None and data["amount"] is None
    try:
        account_id = int(pending.path.split("/")[2])
    except (IndexError, ValueError):
        return False
    if data["requested_account_id"] != account_id:
        return False
    if data["account_id"] is not None and data["account_id"] != account_id:
        return False
    if pending.operation == "CLOSE_ACCOUNT":
        return data["amount"] is None
    amount = pending.payload.get("amount") if pending.payload is not None else None
    return data["amount"] == amount and _positive_integer(amount)


def _nullable_integer(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
