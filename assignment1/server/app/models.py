"""SQLAlchemy ORM models for banking persistence."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base whose metadata is consumed by Alembic."""


class Account(Base):
    """A bank account with database-enforced lifecycle invariants."""

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_accounts_balance_nonnegative"),
        CheckConstraint(
            "name ~ '[^[:space:]]'",
            name="ck_accounts_name_nonempty",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'CLOSED')",
            name="ck_accounts_status_allowed",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND closed_at IS NULL) OR "
            "(status = 'CLOSED' AND closed_at IS NOT NULL)",
            name="ck_accounts_status_closed_at",
        ),
        CheckConstraint(
            "status <> 'CLOSED' OR balance = 0",
            name="ck_accounts_closed_balance_zero",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    balance: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Request(Base):
    """A durable idempotency result and successful-operation history record."""

    __tablename__ = "requests"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('CREATE_ACCOUNT', 'CLOSE_ACCOUNT', 'DEPOSIT', 'WITHDRAW')",
            name="ck_requests_operation_allowed",
        ),
        CheckConstraint(
            "status IN ('SUCCEEDED', 'REJECTED')",
            name="ck_requests_status_allowed",
        ),
        CheckConstraint(
            "payload_hash ~ '^[0-9A-Fa-f]{64}$'",
            name="ck_requests_payload_hash_format",
        ),
        CheckConstraint(
            "((operation = 'CREATE_ACCOUNT' "
            "AND requested_account_id IS NULL AND amount IS NULL) OR "
            "(operation = 'CLOSE_ACCOUNT' "
            "AND requested_account_id IS NOT NULL AND amount IS NULL) OR "
            "(operation IN ('DEPOSIT', 'WITHDRAW') "
            "AND requested_account_id IS NOT NULL AND amount > 0)) IS TRUE",
            name="ck_requests_operation_arguments",
        ),
        CheckConstraint(
            "requested_account_id IS NULL OR account_id IS NULL OR "
            "account_id = requested_account_id",
            name="ck_requests_account_matches_target",
        ),
        CheckConstraint(
            "(balance_before IS NULL OR balance_before >= 0) AND "
            "(balance_after IS NULL OR balance_after >= 0)",
            name="ck_requests_balances_nonnegative",
        ),
        CheckConstraint(
            "((status = 'SUCCEEDED' AND operation = 'CREATE_ACCOUNT' "
            "AND account_id IS NOT NULL AND requested_account_id IS NULL "
            "AND amount IS NULL AND balance_before IS NULL "
            "AND balance_after = 0 AND error_code IS NULL) OR "
            "(status = 'SUCCEEDED' AND operation = 'CLOSE_ACCOUNT' "
            "AND account_id IS NOT NULL AND balance_before = 0 "
            "AND balance_after = 0 AND error_code IS NULL) OR "
            "(status = 'SUCCEEDED' AND operation = 'DEPOSIT' "
            "AND account_id IS NOT NULL AND balance_before IS NOT NULL "
            "AND balance_after IS NOT NULL AND error_code IS NULL "
            "AND balance_after::numeric = "
            "balance_before::numeric + amount::numeric) OR "
            "(status = 'SUCCEEDED' AND operation = 'WITHDRAW' "
            "AND account_id IS NOT NULL AND balance_before IS NOT NULL "
            "AND balance_after IS NOT NULL AND error_code IS NULL "
            "AND balance_after::numeric = "
            "balance_before::numeric - amount::numeric) OR "
            "(status = 'REJECTED' "
            "AND operation IN ('CLOSE_ACCOUNT', 'DEPOSIT', 'WITHDRAW') "
            "AND account_id IS NULL AND balance_before IS NULL "
            "AND balance_after IS NULL AND error_code = 'ACCOUNT_NOT_FOUND') OR "
            "(status = 'REJECTED' "
            "AND operation IN ('CLOSE_ACCOUNT', 'DEPOSIT', 'WITHDRAW') "
            "AND account_id IS NOT NULL AND balance_before = 0 "
            "AND balance_after = 0 "
            "AND error_code = 'ACCOUNT_ALREADY_CLOSED') OR "
            "(status = 'REJECTED' AND operation = 'WITHDRAW' "
            "AND account_id IS NOT NULL AND balance_before IS NOT NULL "
            "AND balance_after IS NOT NULL "
            "AND balance_before = balance_after "
            "AND balance_before::numeric < amount::numeric "
            "AND error_code = 'INSUFFICIENT_FUNDS') OR "
            "(status = 'REJECTED' AND operation = 'CLOSE_ACCOUNT' "
            "AND account_id IS NOT NULL AND balance_before IS NOT NULL "
            "AND balance_after IS NOT NULL "
            "AND balance_before = balance_after AND balance_before > 0 "
            "AND error_code = 'NON_ZERO_BALANCE')) IS TRUE",
            name="ck_requests_result_shape",
        ),
    )

    request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
    )
    requested_account_id: Mapped[int | None] = mapped_column(BigInteger)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id"),
    )
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[int | None] = mapped_column(BigInteger)
    payload_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    balance_before: Mapped[int | None] = mapped_column(BigInteger)
    balance_after: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
