"""Create the durable coordinator transaction journal.

Revision ID: 0001_coordinator_transactions
Revises:
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_coordinator_transactions"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coordinated_transfers",
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_account_id", sa.BigInteger(), nullable=False),
        sa.Column("destination_account_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("payload_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("source_shard", sa.String(length=10), nullable=False),
        sa.Column("destination_shard", sa.String(length=10), nullable=False),
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column("decision", sa.String(length=6), nullable=True),
        sa.Column("source_state", sa.String(length=20), nullable=True),
        sa.Column("destination_state", sa.String(length=20), nullable=True),
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
        sa.CheckConstraint("amount > 0", name="ck_coordinated_transfers_amount"),
        sa.CheckConstraint(
            "state IN ('REGISTERED', 'PREPARING', 'DECIDED', 'RESOLVED')",
            name="ck_coordinated_transfers_state",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('COMMIT', 'ABORT')",
            name="ck_coordinated_transfers_decision",
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
    )


def downgrade() -> None:
    op.drop_table("coordinated_transfers")
