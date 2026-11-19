"""Durable PostgreSQL prepared-transaction participant operations."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import cast
from uuid import UUID

from psycopg import sql
from sqlalchemy import Engine, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from server.app.banking import BankingValidationError
from server.app.models import ParticipantAbortDecision, ParticipantTransaction
from server.app.sharding import ShardDefinition

logger = logging.getLogger(__name__)


class Direction(StrEnum):
    """Local balance change represented by a participant vote."""

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class ParticipantState(StrEnum):
    """Externally observable participant states."""

    PREPARED = "PREPARED"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ParticipantConflict(ValueError):
    """Raised when a transaction UUID is reused with conflicting data or decision."""


@dataclass(frozen=True, slots=True)
class ParticipantResult:
    """One participant vote, decision result, or recovery observation."""

    transaction_id: UUID
    state: ParticipantState
    account_id: int | None = None
    direction: Direction | None = None
    amount: int | None = None
    balance_before: int | None = None
    balance_after: int | None = None
    error_code: str | None = None

    @property
    def vote(self) -> str:
        if self.state in {ParticipantState.PREPARED, ParticipantState.COMMITTED}:
            return "YES"
        if self.state is ParticipantState.REJECTED:
            return "NO"
        return "UNKNOWN"


def prepare(
    session_factory: sessionmaker[Session],
    shard: ShardDefinition,
    transaction_id: UUID,
    account_id: int,
    direction: Direction,
    amount: int,
) -> ParticipantResult:
    """Apply and durably prepare one local debit or credit."""
    _validate(transaction_id, account_id, direction, amount)
    shard.require_owner(account_id)
    gid = _gid(shard.shard_id, transaction_id)
    observed = lookup(session_factory, shard, transaction_id)
    if observed.state is ParticipantState.ABORTED:
        return observed
    if observed.account_id is not None:
        if (
            observed.account_id,
            observed.direction,
            observed.amount,
        ) != (account_id, direction, amount):
            raise ParticipantConflict(
                "Transaction UUID was reused with another payload."
            )
        if observed.state is not ParticipantState.UNKNOWN:
            return observed
    else:
        _ensure_metadata(
            session_factory,
            transaction_id,
            shard.shard_id,
            account_id,
            direction,
            amount,
            gid,
        )

    engine = _engine(session_factory)
    raw = engine.raw_connection()
    prepared = False
    lock_acquired = False
    rejection: tuple[str, int | None] | None = None
    try:
        cursor = raw.cursor()
        cursor.execute(
            "SELECT pg_advisory_lock(%s)",
            (_lock_key(shard.shard_id, transaction_id),),
        )
        lock_acquired = True
        raw.rollback()
        observed = lookup(session_factory, shard, transaction_id)
        if observed.state is not ParticipantState.UNKNOWN:
            return observed

        cursor = raw.cursor()
        cursor.execute(
            "SELECT shard_id, account_id, direction, amount, gid, state "
            "FROM participant_transactions WHERE transaction_id = %s FOR UPDATE",
            (transaction_id,),
        )
        metadata = cursor.fetchone()
        if metadata is None or metadata[:5] != (
            shard.shard_id,
            account_id,
            direction.value,
            amount,
            gid,
        ):
            raw.rollback()
            raise ParticipantConflict(
                "Transaction payload conflicts with durable state."
            )
        if metadata[5] != "PREPARING":
            raw.rollback()
            return lookup(session_factory, shard, transaction_id)

        cursor.execute(
            "SELECT id, balance, status FROM accounts WHERE id = %s FOR UPDATE",
            (account_id,),
        )
        account = cursor.fetchone()
        if account is None:
            rejection = ("ACCOUNT_NOT_FOUND", None)
        elif account[2] != "ACTIVE":
            rejection = ("ACCOUNT_ALREADY_CLOSED", account[1])
        elif direction is Direction.DEBIT and account[1] < amount:
            rejection = ("INSUFFICIENT_FUNDS", account[1])

        if rejection is not None:
            raw.rollback()
            _mark_rejected(session_factory, transaction_id, *rejection)
            return lookup(session_factory, shard, transaction_id)

        balance_before = account[1]
        balance_after = (
            balance_before - amount
            if direction is Direction.DEBIT
            else balance_before + amount
        )
        cursor.execute(
            "UPDATE accounts SET balance = %s WHERE id = %s",
            (balance_after, account_id),
        )
        cursor.execute(
            "UPDATE participant_transactions SET state = 'COMMITTED', "
            "balance_before = %s, balance_after = %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE transaction_id = %s AND state = 'PREPARING'",
            (balance_before, balance_after, transaction_id),
        )
        if cursor.rowcount != 1:
            raw.rollback()
            return lookup(session_factory, shard, transaction_id)
        cursor.execute(sql.SQL("PREPARE TRANSACTION {}").format(sql.Literal(gid)))
        prepared = True
    except ParticipantConflict:
        raise
    except Exception:  # noqa: BLE001 - preserve uncertain transaction outcome
        if not prepared:
            try:
                raw.rollback()
            except Exception:
                logger.warning(
                    "Participant rollback cleanup failed",
                    exc_info=True,
                )
        return lookup(session_factory, shard, transaction_id)
    finally:
        if lock_acquired:
            try:
                raw.rollback()
                cursor = raw.cursor()
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_lock_key(shard.shard_id, transaction_id),),
                )
                raw.commit()
            except Exception:
                logger.warning(
                    "Participant advisory-lock cleanup failed",
                    exc_info=True,
                )
        raw.close()
    return lookup(session_factory, shard, transaction_id)


def commit(
    session_factory: sessionmaker[Session],
    shard: ShardDefinition,
    transaction_id: UUID,
) -> ParticipantResult:
    """Commit a prepared local change exactly once."""
    observed = lookup(session_factory, shard, transaction_id)
    if observed.state is ParticipantState.COMMITTED:
        return observed
    if observed.state in {ParticipantState.ABORTED, ParticipantState.REJECTED}:
        raise ParticipantConflict(
            f"Cannot commit a {observed.state.value} participant."
        )
    if observed.state is not ParticipantState.PREPARED:
        return observed
    resolved = _resolve(session_factory, shard, transaction_id, commit_decision=True)
    if resolved.state in {ParticipantState.ABORTED, ParticipantState.REJECTED}:
        raise ParticipantConflict(
            f"Cannot commit a {resolved.state.value} participant."
        )
    return resolved


def abort(
    session_factory: sessionmaker[Session],
    shard: ShardDefinition,
    transaction_id: UUID,
) -> ParticipantResult:
    """Roll back a prepared local change exactly once."""
    observed = lookup(session_factory, shard, transaction_id)
    if observed.state is ParticipantState.ABORTED:
        return observed
    if observed.state is ParticipantState.COMMITTED:
        raise ParticipantConflict("Cannot abort a committed participant.")
    if observed.state is ParticipantState.REJECTED:
        return observed
    if observed.state is ParticipantState.PREPARED:
        resolved = _resolve(
            session_factory, shard, transaction_id, commit_decision=False
        )
        if resolved.state is ParticipantState.COMMITTED:
            raise ParticipantConflict("Cannot abort a committed participant.")
        return resolved
    if observed.account_id is not None:
        _mark_aborted(session_factory, transaction_id)
        return lookup(session_factory, shard, transaction_id)
    _record_abort_decision(session_factory, transaction_id)
    return lookup(session_factory, shard, transaction_id)


def lookup(
    session_factory: sessionmaker[Session],
    shard: ShardDefinition,
    transaction_id: UUID,
) -> ParticipantResult:
    """Recover durable state without guessing from a missing prepared GID."""
    try:
        with session_factory() as session:
            item = session.get(ParticipantTransaction, transaction_id)
            abort_decided = (
                session.get(ParticipantAbortDecision, transaction_id) is not None
            )
            if item is None:
                state = (
                    ParticipantState.ABORTED
                    if abort_decided
                    else ParticipantState.UNKNOWN
                )
                return ParticipantResult(transaction_id, state)
            if item.shard_id != shard.shard_id:
                return ParticipantResult(transaction_id, ParticipantState.UNKNOWN)
            prepared = session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_prepared_xacts WHERE gid = :gid)"
                ),
                {"gid": item.gid},
            ).scalar_one()
            if item.state == "COMMITTED":
                state = ParticipantState.COMMITTED
            elif item.state == "ABORTED":
                state = ParticipantState.ABORTED
            elif item.state == "REJECTED":
                state = ParticipantState.REJECTED
            elif prepared:
                state = ParticipantState.PREPARED
            elif abort_decided:
                state = ParticipantState.ABORTED
            else:
                state = ParticipantState.UNKNOWN
            return _result(item, state)
    except SQLAlchemyError:
        return ParticipantResult(transaction_id, ParticipantState.UNKNOWN)


def _resolve(
    session_factory: sessionmaker[Session],
    shard: ShardDefinition,
    transaction_id: UUID,
    *,
    commit_decision: bool,
) -> ParticipantResult:
    engine = _engine(session_factory)
    gid = _gid(shard.shard_id, transaction_id)
    raw = engine.raw_connection()
    driver = raw.driver_connection
    lock_acquired = False
    try:
        driver.autocommit = True
        cursor = driver.cursor()
        cursor.execute(
            "SELECT pg_advisory_lock(%s)",
            (_lock_key(shard.shard_id, transaction_id),),
        )
        lock_acquired = True
        observed = lookup(session_factory, shard, transaction_id)
        if observed.state is not ParticipantState.PREPARED:
            return observed
        if commit_decision and _abort_decided(session_factory, transaction_id):
            raise ParticipantConflict("Cannot commit an aborted participant.")
        if not commit_decision:
            _record_abort_decision(session_factory, transaction_id)
        command = "COMMIT PREPARED {}" if commit_decision else "ROLLBACK PREPARED {}"
        cursor.execute(sql.SQL(command).format(sql.Literal(gid)))
        if not commit_decision:
            _mark_aborted(session_factory, transaction_id)
    except ParticipantConflict:
        raise
    except Exception:  # noqa: BLE001 - preserve uncertain transaction outcome
        return lookup(session_factory, shard, transaction_id)
    finally:
        if lock_acquired:
            try:
                cursor = driver.cursor()
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_lock_key(shard.shard_id, transaction_id),),
                )
            except Exception:
                logger.warning(
                    "Participant resolution lock cleanup failed",
                    exc_info=True,
                )
        try:
            driver.autocommit = False
        except Exception:
            logger.warning(
                "Participant connection cleanup failed",
                exc_info=True,
            )
        raw.close()
    return lookup(session_factory, shard, transaction_id)


def _ensure_metadata(
    session_factory: sessionmaker[Session],
    transaction_id: UUID,
    shard_id: str,
    account_id: int,
    direction: Direction,
    amount: int,
    gid: str,
) -> None:
    raw = _engine(session_factory).raw_connection()
    driver = raw.driver_connection
    lock_acquired = False
    try:
        driver.autocommit = True
        cursor = driver.cursor()
        cursor.execute(
            "SELECT pg_advisory_lock(%s)",
            (_lock_key(shard_id, transaction_id),),
        )
        lock_acquired = True
        with session_factory() as session:
            existing = session.get(ParticipantTransaction, transaction_id)
            if existing is not None:
                _require_matching_metadata(
                    existing, shard_id, account_id, direction, amount, gid
                )
                return

        statement = insert(ParticipantTransaction).values(
            transaction_id=transaction_id,
            shard_id=shard_id,
            account_id=account_id,
            direction=direction.value,
            amount=amount,
            gid=gid,
            state="PREPARING",
        )
        with session_factory.begin() as session:
            session.execute(
                statement.on_conflict_do_nothing(index_elements=["transaction_id"])
            )
            item = session.get(ParticipantTransaction, transaction_id)
            if item is None:
                raise RuntimeError("Participant metadata insert was not observable.")
            _require_matching_metadata(
                item, shard_id, account_id, direction, amount, gid
            )
    finally:
        if lock_acquired:
            try:
                cursor = driver.cursor()
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_lock_key(shard_id, transaction_id),),
                )
            except Exception:
                logger.warning(
                    "Participant metadata lock cleanup failed",
                    exc_info=True,
                )
        try:
            driver.autocommit = False
        except Exception:
            logger.warning(
                "Participant metadata connection cleanup failed",
                exc_info=True,
            )
        raw.close()


def _require_matching_metadata(
    item: ParticipantTransaction,
    shard_id: str,
    account_id: int,
    direction: Direction,
    amount: int,
    gid: str,
) -> None:
    if (
        item.shard_id,
        item.account_id,
        item.direction,
        item.amount,
        item.gid,
    ) != (shard_id, account_id, direction.value, amount, gid):
        raise ParticipantConflict("Transaction UUID was reused with another payload.")


def _mark_rejected(
    session_factory: sessionmaker[Session],
    transaction_id: UUID,
    error_code: str,
    balance: int | None,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            update(ParticipantTransaction)
            .where(
                ParticipantTransaction.transaction_id == transaction_id,
                ParticipantTransaction.state == "PREPARING",
            )
            .values(
                state="REJECTED",
                error_code=error_code,
                balance_before=balance,
                balance_after=balance,
                updated_at=text("CURRENT_TIMESTAMP"),
            )
        )


def _mark_aborted(session_factory: sessionmaker[Session], transaction_id: UUID) -> None:
    try:
        with session_factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '1s'"))
            session.execute(
                update(ParticipantTransaction)
                .where(
                    ParticipantTransaction.transaction_id == transaction_id,
                    ParticipantTransaction.state == "PREPARING",
                )
                .values(state="ABORTED", updated_at=text("CURRENT_TIMESTAMP"))
            )
    except SQLAlchemyError:
        return


def _record_abort_decision(
    session_factory: sessionmaker[Session], transaction_id: UUID
) -> None:
    statement = insert(ParticipantAbortDecision).values(transaction_id=transaction_id)
    with session_factory.begin() as session:
        session.execute(
            statement.on_conflict_do_nothing(index_elements=["transaction_id"])
        )


def _abort_decided(
    session_factory: sessionmaker[Session], transaction_id: UUID
) -> bool:
    with session_factory() as session:
        return session.get(ParticipantAbortDecision, transaction_id) is not None


def _result(item: ParticipantTransaction, state: ParticipantState) -> ParticipantResult:
    return ParticipantResult(
        transaction_id=item.transaction_id,
        state=state,
        account_id=item.account_id,
        direction=Direction(item.direction),
        amount=item.amount,
        balance_before=item.balance_before,
        balance_after=item.balance_after,
        error_code=item.error_code,
    )


def _validate(
    transaction_id: UUID, account_id: int, direction: Direction, amount: int
) -> None:
    if not isinstance(transaction_id, UUID):
        raise BankingValidationError("Transaction ID must be a UUID.")
    if (
        isinstance(account_id, bool)
        or not isinstance(account_id, int)
        or account_id <= 0
    ):
        raise BankingValidationError("Account ID must be a positive integer.")
    if not isinstance(direction, Direction):
        raise BankingValidationError("Direction must be DEBIT or CREDIT.")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise BankingValidationError("Amount must be a positive integer.")


def _engine(session_factory: sessionmaker[Session]) -> Engine:
    engine = session_factory.kw.get("bind")
    if not isinstance(engine, Engine):
        raise RuntimeError(  # noqa: TRY004 - invalid internal engine configuration
            "Participant database engine is unavailable."
        )
    return cast(Engine, engine)


def _gid(shard_id: str, transaction_id: UUID) -> str:
    return f"mbts:{shard_id}:{transaction_id}"


def _lock_key(shard_id: str, transaction_id: UUID) -> int:
    digest = hashlib.blake2b(
        f"{shard_id}:{transaction_id}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, signed=True)
