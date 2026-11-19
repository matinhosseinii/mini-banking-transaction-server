"""Create accounts and requests tables.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the initial application schema."""
    op.create_table(
        "accounts",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "balance",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=10),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "balance >= 0",
            name="ck_accounts_balance_nonnegative",
        ),
        sa.CheckConstraint(
            "name ~ '[^[:space:]]'",
            name="ck_accounts_name_nonempty",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'CLOSED')",
            name="ck_accounts_status_allowed",
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND closed_at IS NULL) OR "
            "(status = 'CLOSED' AND closed_at IS NOT NULL)",
            name="ck_accounts_status_closed_at",
        ),
        sa.CheckConstraint(
            "status <> 'CLOSED' OR balance = 0",
            name="ck_accounts_closed_balance_zero",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "requests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_account_id", sa.BigInteger(), nullable=True),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("payload_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("balance_before", sa.BigInteger(), nullable=True),
        sa.Column("balance_after", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation IN ('CREATE_ACCOUNT', 'CLOSE_ACCOUNT', 'DEPOSIT', 'WITHDRAW')",
            name="ck_requests_operation_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('SUCCEEDED', 'REJECTED')",
            name="ck_requests_status_allowed",
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9A-Fa-f]{64}$'",
            name="ck_requests_payload_hash_format",
        ),
        sa.CheckConstraint(
            "((operation = 'CREATE_ACCOUNT' "
            "AND requested_account_id IS NULL AND amount IS NULL) OR "
            "(operation = 'CLOSE_ACCOUNT' "
            "AND requested_account_id IS NOT NULL AND amount IS NULL) OR "
            "(operation IN ('DEPOSIT', 'WITHDRAW') "
            "AND requested_account_id IS NOT NULL AND amount > 0)) IS TRUE",
            name="ck_requests_operation_arguments",
        ),
        sa.CheckConstraint(
            "requested_account_id IS NULL OR account_id IS NULL OR "
            "account_id = requested_account_id",
            name="ck_requests_account_matches_target",
        ),
        sa.CheckConstraint(
            "(balance_before IS NULL OR balance_before >= 0) AND "
            "(balance_after IS NULL OR balance_after >= 0)",
            name="ck_requests_balances_nonnegative",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("request_id"),
    )


def downgrade() -> None:
    """Remove the application schema in dependency-safe order."""
    op.drop_table("requests")
    op.drop_table("accounts")
