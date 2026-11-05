"""Durable idempotency coordination for mutating banking operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from server.app import banking
from server.app.banking import (
    BankingValidationError,
    BusinessErrorCode,
    MutationResult,
    OperationStatus,
)
from server.app.models import Request

_LOCK_NAMESPACE = b"mini-banking:idempotency:v1\x00"


class Operation(StrEnum):
    """Mutating operations supported by durable idempotency."""

    CREATE_ACCOUNT = "CREATE_ACCOUNT"
    CLOSE_ACCOUNT = "CLOSE_ACCOUNT"
    DEPOSIT = "DEPOSIT"
    WITHDRAW = "WITHDRAW"


class IdempotencyConflict(RuntimeError):
    """Raised when a request UUID is reused for a different payload."""

    def __init__(self, request_id: UUID) -> None:
        self.request_id = request_id
        super().__init__(f"Request {request_id} was already used for another payload.")


@dataclass(frozen=True, slots=True)
class RequestResult:
    """Detached representation of a durable request outcome."""

    request_id: UUID
    operation: Operation
    requested_account_id: int | None
    account_id: int | None
    amount: int | None
    payload_hash: str
    status: OperationStatus
    balance_before: int | None
    balance_after: int | None
    error_code: BusinessErrorCode | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CoordinatedResult:
    """A committed request result and whether it was replayed."""

    result: RequestResult
    replayed: bool


@dataclass(frozen=True, slots=True)
class RequestLookup:
    """Explicit result of looking up a committed request UUID."""

    found: bool
    result: RequestResult | None


def coordinate_create_account(
    session_factory: sessionmaker[Session],
    request_id: UUID,
    name: str,
) -> CoordinatedResult:
    """Create an account at most once for a logical request UUID."""
    _validate_request_id(request_id)
    _validate_name(name)
    payload_hash = _canonical_payload_hash(
        {"operation": Operation.CREATE_ACCOUNT, "name": name}
    )
    return _coordinate(
        session_factory=session_factory,
        request_id=request_id,
        operation=Operation.CREATE_ACCOUNT,
        requested_account_id=None,
        amount=None,
        payload_hash=payload_hash,
        execute=lambda session: banking.create_account(session, name),
    )


def coordinate_close_account(
    session_factory: sessionmaker[Session],
    request_id: UUID,
    account_id: int,
) -> CoordinatedResult:
    """Close an account at most once for a logical request UUID."""
    _validate_request_id(request_id)
    _validate_account_id(account_id)
    payload_hash = _canonical_payload_hash(
        {
            "operation": Operation.CLOSE_ACCOUNT,
            "requested_account_id": account_id,
        }
    )
    return _coordinate(
        session_factory=session_factory,
        request_id=request_id,
        operation=Operation.CLOSE_ACCOUNT,
        requested_account_id=account_id,
        amount=None,
        payload_hash=payload_hash,
        execute=lambda session: banking.close_account(session, account_id),
    )


def coordinate_deposit(
    session_factory: sessionmaker[Session],
    request_id: UUID,
    account_id: int,
    amount: int,
) -> CoordinatedResult:
    """Deposit funds at most once for a logical request UUID."""
    _validate_request_id(request_id)
    _validate_account_id(account_id)
    _validate_amount(amount)
    payload_hash = _canonical_payload_hash(
        {
            "operation": Operation.DEPOSIT,
            "requested_account_id": account_id,
            "amount": amount,
        }
    )
    return _coordinate(
        session_factory=session_factory,
        request_id=request_id,
        operation=Operation.DEPOSIT,
        requested_account_id=account_id,
        amount=amount,
        payload_hash=payload_hash,
        execute=lambda session: banking.deposit(session, account_id, amount),
    )


def coordinate_withdraw(
    session_factory: sessionmaker[Session],
    request_id: UUID,
    account_id: int,
    amount: int,
) -> CoordinatedResult:
    """Withdraw funds at most once for a logical request UUID."""
    _validate_request_id(request_id)
    _validate_account_id(account_id)
    _validate_amount(amount)
    payload_hash = _canonical_payload_hash(
        {
            "operation": Operation.WITHDRAW,
            "requested_account_id": account_id,
            "amount": amount,
        }
    )
    return _coordinate(
        session_factory=session_factory,
        request_id=request_id,
        operation=Operation.WITHDRAW,
        requested_account_id=account_id,
        amount=amount,
        payload_hash=payload_hash,
        execute=lambda session: banking.withdraw(session, account_id, amount),
    )


def lookup_request(
    session_factory: sessionmaker[Session],
    request_id: UUID,
) -> RequestLookup:
    """Look up a committed outcome without modifying database state."""
    _validate_request_id(request_id)
    with session_factory() as session:
        request = session.scalar(
            select(Request).where(Request.request_id == request_id)
        )
        if request is None:
            return RequestLookup(found=False, result=None)
        return RequestLookup(found=True, result=_to_result(request))


def _coordinate(
    *,
    session_factory: sessionmaker[Session],
    request_id: UUID,
    operation: Operation,
    requested_account_id: int | None,
    amount: int | None,
    payload_hash: str,
    execute: Callable[[Session], MutationResult],
) -> CoordinatedResult:
    coordinated_result: CoordinatedResult
    with session_factory.begin() as session:
        _acquire_request_lock(session, request_id)
        existing = session.scalar(
            select(Request).where(Request.request_id == request_id)
        )
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise IdempotencyConflict(request_id)
            coordinated_result = CoordinatedResult(
                result=_to_result(existing),
                replayed=True,
            )
        else:
            banking_result = execute(session)
            request = Request(
                request_id=request_id,
                requested_account_id=requested_account_id,
                account_id=banking_result.account_id,
                operation=operation,
                amount=amount,
                payload_hash=payload_hash,
                status=banking_result.status,
                balance_before=banking_result.balance_before,
                balance_after=banking_result.balance_after,
                error_code=banking_result.error_code,
            )
            session.add(request)
            session.flush()
            coordinated_result = CoordinatedResult(
                result=_to_result(request),
                replayed=False,
            )

    return coordinated_result


def _acquire_request_lock(session: Session, request_id: UUID) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _advisory_lock_key(request_id)},
    )


def _advisory_lock_key(request_id: UUID) -> int:
    digest = hashlib.sha256(_LOCK_NAMESPACE + request_id.bytes).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _canonical_payload_hash(payload: dict[str, Any]) -> str:
    """Hash compact sorted-key JSON while preserving exact Unicode strings."""
    canonical_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _to_result(request: Request) -> RequestResult:
    return RequestResult(
        request_id=request.request_id,
        operation=Operation(request.operation),
        requested_account_id=request.requested_account_id,
        account_id=request.account_id,
        amount=request.amount,
        payload_hash=request.payload_hash,
        status=OperationStatus(request.status),
        balance_before=request.balance_before,
        balance_after=request.balance_after,
        error_code=(
            BusinessErrorCode(request.error_code)
            if request.error_code is not None
            else None
        ),
        created_at=request.created_at,
    )


def _validate_request_id(request_id: UUID) -> None:
    if not isinstance(request_id, UUID):
        raise BankingValidationError("Request ID must be a UUID.")


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise BankingValidationError("Account name must contain non-whitespace.")
    if len(name) > 100:
        raise BankingValidationError("Account name must not exceed 100 characters.")


def _validate_account_id(account_id: int) -> None:
    if (
        isinstance(account_id, bool)
        or not isinstance(account_id, int)
        or account_id <= 0
    ):
        raise BankingValidationError("Account ID must be a positive integer.")


def _validate_amount(amount: int) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise BankingValidationError("Amount must be a positive integer.")
