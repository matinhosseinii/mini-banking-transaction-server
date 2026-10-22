"""Bounded concurrent HTTP execution and result classification."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import httpx

from client.config import Timeouts


class Outcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class Consistency(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class OperationResult:
    index: int
    operation: str
    outcome: Outcome
    request_id: str | None
    status_code: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class RunResult:
    scenario: str
    service: str
    account_id: int
    total_requests: int
    concurrency: int
    amount: int | None
    elapsed_seconds: float
    succeeded: int
    rejected: int
    unknown: int
    errors: int
    initial_balance: int | None
    expected_balance: int | None
    actual_balance: int | None
    consistency: Consistency | None
    operations: list[OperationResult]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_BUSINESS_ERRORS = {
    "ACCOUNT_NOT_FOUND",
    "ACCOUNT_ALREADY_CLOSED",
    "INSUFFICIENT_FUNDS",
}


def _timeout(timeouts: Timeouts) -> httpx.Timeout:
    return httpx.Timeout(
        connect=timeouts.connect,
        read=timeouts.read,
        write=timeouts.write,
        pool=timeouts.pool,
    )


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        value = response.json()
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _valid_balance(response: httpx.Response, account_id: int) -> int | None:
    if not 200 <= response.status_code < 300:
        return None
    data = _json_object(response)
    if data is None or data.get("account_id") != account_id:
        return None
    balance = data.get("balance")
    if isinstance(balance, bool) or not isinstance(balance, int) or balance < 0:
        return None
    return balance


async def _read_balance(
    client: httpx.AsyncClient, account_id: int
) -> tuple[int | None, OperationResult | None]:
    try:
        response = await client.get(f"/accounts/{account_id}/balance")
    except httpx.RequestError:
        return None, OperationResult(
            -1, "BALANCE", Outcome.ERROR, None, None, "Balance request failed."
        )
    balance = _valid_balance(response, account_id)
    if balance is None:
        return None, OperationResult(
            -1,
            "BALANCE",
            Outcome.ERROR,
            None,
            response.status_code,
            "Invalid or unsuccessful balance response.",
        )
    return balance, None


def _mutation_result(
    index: int,
    operation: str,
    request_id: UUID,
    response: httpx.Response,
    account_id: int,
    amount: int,
) -> OperationResult:
    request_id_text = str(request_id)
    data = _json_object(response)
    if response.status_code >= 500:
        return OperationResult(
            index,
            operation,
            Outcome.UNKNOWN,
            request_id_text,
            response.status_code,
            "Mutation outcome is unknown after a server failure.",
        )
    if 300 <= response.status_code < 400:
        return OperationResult(
            index,
            operation,
            Outcome.UNKNOWN,
            request_id_text,
            response.status_code,
            "Mutation outcome is unknown after a redirect response.",
        )
    if 200 <= response.status_code < 300:
        valid = (
            data is not None
            and data.get("request_id") == request_id_text
            and data.get("operation") == operation
            and data.get("requested_account_id") == account_id
            and data.get("account_id") == account_id
            and data.get("amount") == amount
            and data.get("status") == "SUCCEEDED"
            and isinstance(data.get("balance_after"), int)
            and not isinstance(data.get("balance_after"), bool)
            and data["balance_after"] >= 0
        )
        return OperationResult(
            index,
            operation,
            Outcome.SUCCEEDED if valid else Outcome.UNKNOWN,
            request_id_text,
            response.status_code,
            "Succeeded." if valid else "Mutation response was ambiguous.",
        )
    valid_rejection = (
        data is not None
        and data.get("request_id") == request_id_text
        and data.get("error_code") in _BUSINESS_ERRORS
    )
    if valid_rejection:
        return OperationResult(
            index,
            operation,
            Outcome.REJECTED,
            request_id_text,
            response.status_code,
            str(data.get("detail", "Rejected.")),
        )
    return OperationResult(
        index,
        operation,
        Outcome.ERROR,
        request_id_text,
        response.status_code,
        "Mutation request was rejected before a banking outcome was confirmed.",
    )


async def _execute_operation(
    client: httpx.AsyncClient,
    scenario: str,
    index: int,
    account_id: int,
    amount: int | None,
) -> OperationResult:
    if scenario == "balance":
        balance, error = await _read_balance(client, account_id)
        if error is not None:
            return OperationResult(
                index, error.operation, error.outcome, None, error.status_code, error.detail
            )
        assert balance is not None
        return OperationResult(
            index, "BALANCE", Outcome.SUCCEEDED, None, 200, "Succeeded."
        )

    assert amount is not None
    operation = (
        "DEPOSIT" if scenario == "deposit" or scenario == "mixed" and index % 2 == 0
        else "WITHDRAW"
    )
    request_id = uuid4()
    try:
        response = await client.post(
            f"/accounts/{account_id}/{operation.lower()}",
            json={"amount": amount},
            headers={"Idempotency-Key": str(request_id)},
        )
    except httpx.RequestError:
        return OperationResult(
            index,
            operation,
            Outcome.UNKNOWN,
            str(request_id),
            None,
            "Mutation outcome is unknown after a transport failure.",
        )
    return _mutation_result(index, operation, request_id, response, account_id, amount)


async def run_generator(
    *,
    base_url: str,
    service: str,
    scenario: str,
    account_id: int,
    requests: int,
    concurrency: int,
    amount: int | None,
    timeouts: Timeouts,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    """Run a scenario with a fixed number of workers and no retries."""
    started = clock()
    limits = httpx.Limits(max_connections=concurrency)
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=_timeout(timeouts),
        follow_redirects=False,
        limits=limits,
        transport=transport,
    ) as client:
        initial_balance = None
        initial_error = None
        if scenario != "balance":
            initial_balance, initial_error = await _read_balance(client, account_id)

        results: list[OperationResult | None] = [None] * requests
        next_index = 0
        index_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(concurrency)

        async def worker() -> None:
            nonlocal next_index
            while True:
                async with index_lock:
                    if next_index >= requests:
                        return
                    index = next_index
                    next_index += 1
                async with semaphore:
                    results[index] = await _execute_operation(
                        client, scenario, index, account_id, amount
                    )

        await asyncio.gather(*(worker() for _ in range(min(concurrency, requests))))
        final_balance = None
        final_error = None
        if scenario != "balance":
            final_balance, final_error = await _read_balance(client, account_id)

    completed = [result for result in results if result is not None]
    succeeded = sum(result.outcome is Outcome.SUCCEEDED for result in completed)
    rejected = sum(result.outcome is Outcome.REJECTED for result in completed)
    unknown = sum(result.outcome is Outcome.UNKNOWN for result in completed)
    errors = sum(result.outcome is Outcome.ERROR for result in completed)

    expected_balance = None
    consistency = None
    if scenario != "balance":
        if initial_balance is not None:
            deposits = sum(
                amount or 0
                for result in completed
                if result.outcome is Outcome.SUCCEEDED
                and result.operation == "DEPOSIT"
            )
            withdrawals = sum(
                amount or 0
                for result in completed
                if result.outcome is Outcome.SUCCEEDED
                and result.operation == "WITHDRAW"
            )
            expected_balance = initial_balance + deposits - withdrawals
        if (
            unknown
            or initial_error is not None
            or final_error is not None
            or expected_balance is None
            or final_balance is None
        ):
            consistency = Consistency.INCONCLUSIVE
        elif expected_balance == final_balance:
            consistency = Consistency.PASS
        else:
            consistency = Consistency.FAIL

    return RunResult(
        scenario=scenario.upper(),
        service=service,
        account_id=account_id,
        total_requests=requests,
        concurrency=concurrency,
        amount=amount,
        elapsed_seconds=round(clock() - started, 6),
        succeeded=succeeded,
        rejected=rejected,
        unknown=unknown,
        errors=errors,
        initial_balance=initial_balance,
        expected_balance=expected_balance,
        actual_balance=final_balance,
        consistency=consistency,
        operations=completed,
    )
