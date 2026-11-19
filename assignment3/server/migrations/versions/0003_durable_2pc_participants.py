"""Add durable state for PostgreSQL 2PC participants.

Revision ID: 0003_durable_2pc_participants
Revises: 0002_same_shard_transfers
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_durable_2pc_participants"
down_revision: str | None = "0002_same_shard_transfers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the participant transaction journal."""
    op.create_table(
        "participant_transactions",
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shard_id", sa.String(length=10), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("gid", sa.String(length=200), nullable=False),
        sa.Column(
            "state", sa.String(length=12), server_default="PREPARING", nullable=False
        ),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("balance_before", sa.BigInteger(), nullable=True),
        sa.Column("balance_after", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "direction IN ('DEBIT', 'CREDIT')",
            name="ck_participant_transactions_direction",
        ),
        sa.CheckConstraint("amount > 0", name="ck_participant_transactions_amount"),
        sa.CheckConstraint(
            "state IN ('PREPARING', 'COMMITTED', 'ABORTED', 'REJECTED')",
            name="ck_participant_transactions_state",
        ),
        sa.CheckConstraint(
            "(state = 'REJECTED' AND error_code IS NOT NULL) OR "
            "(state <> 'REJECTED' AND error_code IS NULL)",
            name="ck_participant_transactions_error",
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
        sa.UniqueConstraint("gid"),
    )
    op.create_table(
        "participant_abort_decisions",
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
    )


def downgrade() -> None:
    """Remove participant state after prepared transactions are resolved."""
    op.drop_table("participant_abort_decisions")
    op.drop_table("participant_transactions")
