"""Correlation and structured HTTP completion logging middleware."""

from __future__ import annotations

import logging
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server.app.errors import problem_response
from server.app.logging_config import correlation_id_context

logger = logging.getLogger("server.app.http")
_MUTATING_PATHS = {
    "/accounts",
    "/accounts/{account_id}",
    "/accounts/{account_id}/deposit",
    "/accounts/{account_id}/withdraw",
}
_ACTIONS = {
    ("GET", "/ready"): "READINESS",
    ("POST", "/accounts"): "CREATE_ACCOUNT",
    ("DELETE", "/accounts/{account_id}"): "CLOSE_ACCOUNT",
    ("GET", "/accounts/{account_id}/balance"): "GET_BALANCE",
    ("POST", "/accounts/{account_id}/deposit"): "DEPOSIT",
    ("POST", "/accounts/{account_id}/withdraw"): "WITHDRAW",
    ("GET", "/requests/{request_id}"): "GET_REQUEST",
    ("GET", "/requests"): "LIST_REQUESTS",
}


class RequestLoggingMiddleware:
    """Add correlation headers and emit one safe completion event."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = str(uuid4())
        token = correlation_id_context.set(correlation_id)
        state = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id
        valid_idempotency_key = _valid_idempotency_key(scope)
        started = monotonic()
        status_code = 500
        response_started = False

        async def send_with_correlation(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        except Exception as error:
            status_code = 500
            state["log_classification"] = "internal_error"
            logger.error(
                "Unexpected request failure",
                extra={
                    "event": "unexpected_internal_failure",
                    "exception_type": type(error).__name__,
                },
            )
            if response_started:
                raise
            response = problem_response(
                500,
                "An unexpected internal failure occurred.",
                headers={"X-Request-ID": correlation_id},
            )
            await response(scope, receive, send)
        finally:
            duration_ms = round((monotonic() - started) * 1000, 3)
            path = _normalized_path(scope)
            method = scope["method"]
            if not (method == "GET" and path == "/ready" and status_code == 200):
                metadata = {
                    "event": "http_request_completed",
                    "request_id": correlation_id,
                    "correlation_id": correlation_id,
                    "client_ip": _client_ip(scope),
                    "method": method,
                    "path": path,
                    "action": _ACTIONS.get((method, path), "UNSUPPORTED"),
                    "account_id": _account_id(scope, state),
                    "status_code": status_code,
                    "result": _result(status_code, state),
                    "duration_ms": duration_ms,
                }
                if path in _MUTATING_PATHS and valid_idempotency_key is not None:
                    metadata["idempotency_key"] = valid_idempotency_key
                for field in (
                    "operation",
                    "business_status",
                    "replayed",
                    "error_code",
                ):
                    if field in state:
                        metadata[field] = state[field]
                logger.log(
                    _completion_level(status_code, state),
                    "HTTP request completed",
                    extra=metadata,
                )
            correlation_id_context.reset(token)


def _valid_idempotency_key(scope: Scope) -> str | None:
    headers = dict(scope.get("headers", []))
    raw_value = headers.get(b"idempotency-key")
    if raw_value is None:
        return None
    try:
        return str(UUID(raw_value.decode("ascii")))
    except (UnicodeDecodeError, ValueError):
        return None


def _normalized_path(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "<unmatched>"


def _client_ip(scope: Scope) -> str | None:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        host = client[0]
        return host if isinstance(host, str) else None
    return None


def _account_id(scope: Scope, state: dict[str, Any]) -> int | None:
    value = state.get("account_id")
    if value is None:
        path_params = scope.get("path_params")
        if isinstance(path_params, dict):
            value = path_params.get("account_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _result(status_code: int, state: dict[str, Any]) -> str:
    business_status = state.get("business_status")
    if isinstance(business_status, str):
        return business_status
    classification = state.get("log_classification")
    if isinstance(classification, str):
        return classification.upper()
    return "SUCCEEDED" if status_code < 400 else "ERROR"


def _completion_level(status_code: int, state: dict[str, Any]) -> int:
    if state.get("business_status") == "REJECTED":
        return logging.INFO
    classification = state.get("log_classification")
    if classification in {"infrastructure_error", "internal_error"}:
        return logging.ERROR
    if classification in {"validation_error", "idempotency_conflict", "client_error"}:
        return logging.WARNING
    if status_code >= 500:
        return logging.ERROR
    if status_code >= 400:
        return logging.WARNING
    return logging.INFO
