"""Transaction-safe banking operations independent of the HTTP layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from server.app.models import Account


class OperationStatus(StrEnum):
    """Persistable outcomes for mutating banking operations."""

    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"


class BusinessErrorCode(StrEnum):
    """Persistable business rejection codes."""

    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ACCOUNT_ALREADY_CLOSED = "ACCOUNT_ALREADY_CLOSED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    NON_ZERO_BALANCE = "NON_ZERO_BALANCE"


class AccountStatus(StrEnum):
    """Supported account lifecycle states."""

    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class BankingValidationError(ValueError):
    """Raised when an operation's caller-supplied input is invalid."""


@dataclass(frozen=True, slots=True)
class MutationResult:
    """Outcome data required for later durable request persistence."""

    status: OperationStatus
    account_id: int | None
    balance_before: int | None
    balance_after: int | None
    error_code: BusinessErrorCode | None = None


@dataclass(frozen=True, slots=True)
class BalanceResult:
    """Current state returned by a balance lookup."""

    account_id: int | None
    balance: int | None
    status: AccountStatus | None
    error_code: BusinessErrorCode | None = None


def create_account(session: Session, name: str) -> MutationResult:
    """Create an active, zero-balance account in the caller's transaction."""
    if not isinstance(name, str) or not name.strip():
        raise BankingValidationError("Account name must contain non-whitespace.")

    account = Account(
        name=name,
        balance=0,
        status=AccountStatus.ACTIVE,
        closed_at=None,
    )
    session.add(account)
    session.flush()
    return MutationResult(
        status=OperationStatus.SUCCEEDED,
        account_id=account.id,
        balance_before=None,
        balance_after=0,
    )


def close_account(session: Session, account_id: int) -> MutationResult:
    """Close a zero-balance account while retaining its row lock."""
    account = _locked_account(session, account_id)
    if account is None:
        return _not_found()

    balance = account.balance
    if account.status == AccountStatus.CLOSED:
        return _rejected(
            account.id,
            balance,
            BusinessErrorCode.ACCOUNT_ALREADY_CLOSED,
        )
    if balance != 0:
        return _rejected(
            account.id,
            balance,
            BusinessErrorCode.NON_ZERO_BALANCE,
        )

    account.status = AccountStatus.CLOSED
    account.closed_at = datetime.now(UTC)
    session.flush()
    return _succeeded(account.id, balance, balance)


def deposit(session: Session, account_id: int, amount: int) -> MutationResult:
    """Atomically add a positive amount to an active account."""
    _validate_amount(amount)
    account = _locked_account(session, account_id)
    if account is None:
        return _not_found()

    balance_before = account.balance
    if account.status == AccountStatus.CLOSED:
        return _rejected(
            account.id,
            balance_before,
            BusinessErrorCode.ACCOUNT_ALREADY_CLOSED,
        )

    balance_after = session.execute(
        update(Account)
        .where(Account.id == account.id)
        .values(balance=Account.balance + amount)
        .returning(Account.balance)
    ).scalar_one()
    return _succeeded(account.id, balance_before, balance_after)


def withdraw(session: Session, account_id: int, amount: int) -> MutationResult:
    """Atomically subtract a positive amount when sufficient funds exist."""
    _validate_amount(amount)
    account = _locked_account(session, account_id)
    if account is None:
        return _not_found()

    balance_before = account.balance
    if account.status == AccountStatus.CLOSED:
        return _rejected(
            account.id,
            balance_before,
            BusinessErrorCode.ACCOUNT_ALREADY_CLOSED,
        )
    if balance_before < amount:
        return _rejected(
            account.id,
            balance_before,
            BusinessErrorCode.INSUFFICIENT_FUNDS,
        )

    balance_after = session.execute(
        update(Account)
        .where(Account.id == account.id)
        .values(balance=Account.balance - amount)
        .returning(Account.balance)
    ).scalar_one()
    return _succeeded(account.id, balance_before, balance_after)


def get_balance(session: Session, account_id: int) -> BalanceResult:
    """Return the current state of an active or closed account."""
    row = session.execute(
        select(Account.id, Account.balance, Account.status).where(
            Account.id == account_id
        )
    ).one_or_none()
    if row is None:
        return BalanceResult(
            account_id=None,
            balance=None,
            status=None,
            error_code=BusinessErrorCode.ACCOUNT_NOT_FOUND,
        )
    return BalanceResult(
        account_id=row.id,
        balance=row.balance,
        status=AccountStatus(row.status),
    )


def _locked_account(session: Session, account_id: int) -> Account | None:
    statement = (
        select(Account)
        .where(Account.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalar(statement)


def _validate_amount(amount: int) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise BankingValidationError("Amount must be a positive integer.")


def _not_found() -> MutationResult:
    return MutationResult(
        status=OperationStatus.REJECTED,
        account_id=None,
        balance_before=None,
        balance_after=None,
        error_code=BusinessErrorCode.ACCOUNT_NOT_FOUND,
    )


def _rejected(
    account_id: int,
    balance: int,
    error_code: BusinessErrorCode,
) -> MutationResult:
    return MutationResult(
        status=OperationStatus.REJECTED,
        account_id=account_id,
        balance_before=balance,
        balance_after=balance,
        error_code=error_code,
    )


def _succeeded(
    account_id: int,
    balance_before: int,
    balance_after: int,
) -> MutationResult:
    return MutationResult(
        status=OperationStatus.SUCCEEDED,
        account_id=account_id,
        balance_before=balance_before,
        balance_after=balance_after,
    )
