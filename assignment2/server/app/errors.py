"""Central RFC 9457 error handling for the HTTP API."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError, TimeoutError
from starlette.exceptions import HTTPException

from server.app.banking import BankingValidationError, BusinessErrorCode
from server.app.idempotency import CoordinatedResult, IdempotencyConflict

logger = logging.getLogger(__name__)
PROBLEM_MEDIA_TYPE = "application/problem+json"

_BUSINESS_PROBLEMS = {
    BusinessErrorCode.ACCOUNT_NOT_FOUND: (404, "Account not found."),
    BusinessErrorCode.ACCOUNT_ALREADY_CLOSED: (409, "Account is already closed."),
    BusinessErrorCode.INSUFFICIENT_FUNDS: (409, "Insufficient funds."),
    BusinessErrorCode.NON_ZERO_BALANCE: (409, "Account balance must be zero."),
}
_TEMPORARY_SQLSTATES = {
    "08000",
    "08001",
    "08003",
    "08004",
    "08006",
    "08007",
    "08P01",
    "55P03",
    "57014",
    "57P01",
    "57P02",
    "57P03",
}


def problem_response(
    status_code: int,
    detail: str,
    *,
    headers: Mapping[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    """Build an unwrapped RFC 9457 response."""
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status_code).phrase,
        "status": status_code,
        "detail": detail,
    }
    body.update({key: value for key, value in extensions.items() if value is not None})
    return JSONResponse(
        body,
        status_code=status_code,
        headers=headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def business_problem(outcome: CoordinatedResult) -> JSONResponse:
    """Map a persisted business rejection to its public problem."""
    error_code = outcome.result.error_code
    if error_code is None or error_code not in _BUSINESS_PROBLEMS:
        logger.error("Rejected request has no recognized business error code")
        return problem_response(500, "An unexpected internal failure occurred.")
    status_code, detail = _BUSINESS_PROBLEMS[error_code]
    return problem_response(
        status_code,
        detail,
        error_code=error_code.value,
        request_id=str(outcome.result.request_id),
        replayed=outcome.replayed,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all API exception-to-problem mappings."""

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        request.state.log_classification = "validation_error"
        errors = [
            {
                "loc": list(item["loc"]),
                "msg": item["msg"],
                "type": item["type"],
            }
            for item in error.errors()
        ]
        return problem_response(422, "Request validation failed.", errors=errors)

    @app.exception_handler(BankingValidationError)
    async def banking_validation_handler(
        request: Request, error: BankingValidationError
    ) -> JSONResponse:
        request.state.log_classification = "validation_error"
        return problem_response(422, str(error))

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict_handler(
        request: Request, error: IdempotencyConflict
    ) -> JSONResponse:
        request.state.log_classification = "idempotency_conflict"
        return problem_response(
            409,
            "The Idempotency-Key was already used for a different request.",
            error_code="IDEMPOTENCY_CONFLICT",
            request_id=str(error.request_id),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, error: HTTPException
    ) -> JSONResponse:
        request.state.log_classification = (
            "infrastructure_error" if error.status_code >= 500 else "client_error"
        )
        detail = error.detail if isinstance(error.detail, str) else "Request failed."
        return problem_response(error.status_code, detail, headers=error.headers)

    @app.exception_handler(SQLAlchemyError)
    async def sqlalchemy_handler(
        request: Request, error: SQLAlchemyError
    ) -> JSONResponse:
        temporary = _is_temporary_database_error(error)
        request.state.log_classification = (
            "infrastructure_error" if temporary else "internal_error"
        )
        logger.error(
            "Database operation failed",
            extra={
                "event": "database_operation_failed",
                "exception_type": type(error).__name__,
            },
        )
        if temporary:
            return problem_response(503, "Database is temporarily unavailable.")
        return problem_response(500, "An unexpected internal failure occurred.")

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, error: Exception) -> JSONResponse:
        request.state.log_classification = "internal_error"
        logger.error(
            "Unexpected HTTP request failure",
            extra={
                "event": "unexpected_internal_failure",
                "exception_type": type(error).__name__,
            },
        )
        return problem_response(500, "An unexpected internal failure occurred.")


def _is_temporary_database_error(error: SQLAlchemyError) -> bool:
    if isinstance(error, (OperationalError, TimeoutError)):
        return True
    if not isinstance(error, DBAPIError):
        return False
    if error.connection_invalidated:
        return True
    original = error.orig
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate is None:
        diagnostic = getattr(original, "diag", None)
        sqlstate = getattr(diagnostic, "sqlstate", None)
    return sqlstate in _TEMPORARY_SQLSTATES
