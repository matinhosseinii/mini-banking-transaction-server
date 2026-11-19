"""Coordinator-owned durable transaction journal."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, BigInteger, CheckConstraint, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Coordinator declarative model base."""


class CoordinatedTransfer(Base):
    """Durable identity, votes, and final decision for one transfer."""

    __tablename__ = "coordinated_transfers"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_coordinated_transfers_amount"),
        CheckConstraint(
            "state IN ('REGISTERED', 'PREPARING', 'DECIDED', 'RESOLVED')",
            name="ck_coordinated_transfers_state",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('COMMIT', 'ABORT')",
            name="ck_coordinated_transfers_decision",
        ),
    )

    transaction_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    source_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    destination_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_shard: Mapped[str] = mapped_column(String(10), nullable=False)
    destination_shard: Mapped[str] = mapped_column(String(10), nullable=False)
    state: Mapped[str] = mapped_column(String(12), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(6))
    source_state: Mapped[str | None] = mapped_column(String(20))
    destination_state: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(40))
    balance_before: Mapped[int | None] = mapped_column(BigInteger)
    balance_after: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
