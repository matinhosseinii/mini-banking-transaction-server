"""PostgreSQL integration tests for durable 2PC participant behavior."""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from server.app import participant
from server.app.banking import close_account, create_account, deposit, get_balance
from server.app.models import Account
from server.app.sharding import WrongShardError, get_shard

ALEMBIC_INI = "server/alembic.ini"
SHARD_A = get_shard("A")


@contextmanager
def _temporary_database(admin_url: str) -> Generator[str, None, None]:
    database_name = f"participant_test_{uuid4().hex}"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    database_url = make_url(admin_url).set(database=database_name)
    created = False
    try:
        with admin_engine.connect() as connection:
            max_prepared = connection.execute(
                text("SHOW max_prepared_transactions")
            ).scalar_one()
            if int(max_prepared) == 0:
                pytest.skip("PostgreSQL max_prepared_transactions must be nonzero")
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
            created = True
        yield database_url.render_as_string(hide_password=False)
    finally:
        if created:
            database_engine = create_engine(
                database_url.render_as_string(hide_password=False)
            )
            try:
                with database_engine.connect() as connection:
                    gids = connection.execute(
                        text(
                            "SELECT gid FROM pg_prepared_xacts "
                            "WHERE database = current_database()"
                        )
                    ).scalars()
                    unresolved = list(gids)
                raw = database_engine.raw_connection()
                driver = raw.driver_connection
                driver.autocommit = True
                try:
                    with driver.cursor() as cursor:
                        for gid in unresolved:
                            cursor.execute(
                                sql.SQL("ROLLBACK PREPARED {}").format(sql.Literal(gid))
                            )
                finally:
                    driver.autocommit = False
                    raw.close()
            finally:
                database_engine.dispose()
            with admin_engine.connect() as connection:
                connection.exec_driver_sql(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_engine.dispose()


@pytest.fixture(scope="module")
def participant_database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    admin_url = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if admin_url is None:
        pytest.skip("TEST_POSTGRES_ADMIN_URL is required for participant tests")
    with _temporary_database(admin_url) as database_url:
        config = Config(ALEMBIC_INI)
        config.attributes["database_url"] = database_url
        command.upgrade(config, "head")
        engine = create_engine(database_url, pool_size=10, max_overflow=5)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            yield engine, factory
        finally:
            engine.dispose()


def _account(factory: sessionmaker[Session], balance: int = 0) -> int:
    with factory.begin() as session:
        created = create_account(session, f"Participant {uuid4()}")
    assert created.account_id is not None
    if balance:
        with factory.begin() as session:
            deposit(session, created.account_id, balance)
    return created.account_id


def _balance(factory: sessionmaker[Session], account_id: int) -> int:
    with factory() as session:
        result = get_balance(session, account_id)
    assert result.balance is not None
    return result.balance


def _prepared_count(factory: sessionmaker[Session], transaction_id: UUID) -> int:
    with factory() as session:
        return session.execute(
            text(
                "SELECT count(*) FROM pg_prepared_xacts "
                "WHERE gid = :gid AND database = current_database()"
            ),
            {"gid": f"mbts:A:{transaction_id}"},
        ).scalar_one()


def test_debit_prepare_is_invisible_until_exactly_once_commit_and_restart(
    participant_database,
) -> None:
    engine, factory = participant_database
    account_id = _account(factory, 100)
    transaction_id = uuid4()

    prepared = participant.prepare(
        factory, SHARD_A, transaction_id, account_id, participant.Direction.DEBIT, 40
    )

    assert prepared.state is participant.ParticipantState.PREPARED
    assert prepared.vote == "YES"
    assert _balance(factory, account_id) == 100
    restarted_factory = sessionmaker(bind=engine, expire_on_commit=False)
    assert (
        participant.lookup(restarted_factory, SHARD_A, transaction_id).state
        is participant.ParticipantState.PREPARED
    )
    assert (
        participant.commit(restarted_factory, SHARD_A, transaction_id).state
        is participant.ParticipantState.COMMITTED
    )
    assert (
        participant.commit(factory, SHARD_A, transaction_id).state
        is participant.ParticipantState.COMMITTED
    )
    assert _balance(factory, account_id) == 60
    assert _prepared_count(factory, transaction_id) == 0
    with pytest.raises(participant.ParticipantConflict):
        participant.abort(factory, SHARD_A, transaction_id)


def test_credit_prepare_and_duplicate_abort_leave_balance_unchanged(
    participant_database,
) -> None:
    _, factory = participant_database
    account_id = _account(factory, 10)
    transaction_id = uuid4()

    first = participant.prepare(
        factory, SHARD_A, transaction_id, account_id, participant.Direction.CREDIT, 25
    )
    duplicate = participant.prepare(
        factory, SHARD_A, transaction_id, account_id, participant.Direction.CREDIT, 25
    )
    aborted = participant.abort(factory, SHARD_A, transaction_id)

    assert first.state is participant.ParticipantState.PREPARED
    assert duplicate.state is participant.ParticipantState.PREPARED
    assert aborted.state is participant.ParticipantState.ABORTED
    assert (
        participant.abort(factory, SHARD_A, transaction_id).state
        is participant.ParticipantState.ABORTED
    )
    assert _balance(factory, account_id) == 10
    assert _prepared_count(factory, transaction_id) == 0
    with pytest.raises(participant.ParticipantConflict):
        participant.commit(factory, SHARD_A, transaction_id)


def test_prepare_rejections_and_payload_conflict(participant_database) -> None:
    _, factory = participant_database
    poor_account = _account(factory, 5)
    missing_account = 999
    closed_account = _account(factory)
    with factory.begin() as session:
        close_account(session, closed_account)

    for account_id, error_code in (
        (poor_account, "INSUFFICIENT_FUNDS"),
        (missing_account, "ACCOUNT_NOT_FOUND"),
        (closed_account, "ACCOUNT_ALREADY_CLOSED"),
    ):
        result = participant.prepare(
            factory,
            SHARD_A,
            uuid4(),
            account_id,
            participant.Direction.DEBIT,
            10,
        )
        assert result.state is participant.ParticipantState.REJECTED
        assert result.vote == "NO"
        assert result.error_code == error_code

    transaction_id = uuid4()
    participant.prepare(
        factory,
        SHARD_A,
        transaction_id,
        poor_account,
        participant.Direction.CREDIT,
        10,
    )
    with pytest.raises(participant.ParticipantConflict):
        participant.prepare(
            factory,
            SHARD_A,
            transaction_id,
            poor_account,
            participant.Direction.CREDIT,
            11,
        )
    participant.abort(factory, SHARD_A, transaction_id)
    with pytest.raises(WrongShardError):
        participant.prepare(
            factory,
            SHARD_A,
            uuid4(),
            1001,
            participant.Direction.CREDIT,
            1,
        )


def test_concurrent_replica_prepare_creates_one_prepared_transaction(
    participant_database,
) -> None:
    engine, factory = participant_database
    other_replica = sessionmaker(bind=engine, expire_on_commit=False)
    account_id = _account(factory, 30)
    transaction_id = uuid4()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda local_factory: participant.prepare(
                    local_factory,
                    SHARD_A,
                    transaction_id,
                    account_id,
                    participant.Direction.DEBIT,
                    10,
                ),
                (factory, other_replica),
            )
        )

    assert participant.ParticipantState.PREPARED in {item.state for item in results}
    assert _prepared_count(factory, transaction_id) == 1
    participant.commit(factory, SHARD_A, transaction_id)
    assert _balance(factory, account_id) == 20
    assert _prepared_count(factory, transaction_id) == 0


def test_committed_marker_recovers_after_response_loss(participant_database) -> None:
    engine, factory = participant_database
    account_id = _account(factory, 20)
    transaction_id = uuid4()
    participant.prepare(
        factory, SHARD_A, transaction_id, account_id, participant.Direction.DEBIT, 5
    )

    raw = engine.raw_connection()
    driver = raw.driver_connection
    driver.autocommit = True
    try:
        with driver.cursor() as cursor:
            cursor.execute(
                sql.SQL("COMMIT PREPARED {}").format(
                    sql.Literal(f"mbts:A:{transaction_id}")
                )
            )
    finally:
        driver.autocommit = False
        raw.close()

    recovered = participant.lookup(factory, SHARD_A, transaction_id)
    assert recovered.state is participant.ParticipantState.COMMITTED
    assert _balance(factory, account_id) == 15
    assert _prepared_count(factory, transaction_id) == 0
    with factory() as session:
        assert session.scalar(select(Account.id).where(Account.id == account_id))


def test_abort_tombstone_recovers_before_state_update(
    participant_database, monkeypatch
) -> None:
    _, factory = participant_database
    account_id = _account(factory, 20)
    transaction_id = uuid4()
    participant.prepare(
        factory, SHARD_A, transaction_id, account_id, participant.Direction.DEBIT, 5
    )
    monkeypatch.setattr(participant, "_mark_aborted", lambda *args: None)

    aborted = participant.abort(factory, SHARD_A, transaction_id)

    assert aborted.state is participant.ParticipantState.ABORTED
    assert _balance(factory, account_id) == 20
    assert _prepared_count(factory, transaction_id) == 0
    with pytest.raises(participant.ParticipantConflict):
        participant.commit(factory, SHARD_A, transaction_id)


def test_abort_decision_survives_crash_before_rollback(participant_database) -> None:
    _, factory = participant_database
    account_id = _account(factory, 20)
    transaction_id = uuid4()
    participant.prepare(
        factory, SHARD_A, transaction_id, account_id, participant.Direction.DEBIT, 5
    )

    participant._record_abort_decision(factory, transaction_id)

    assert (
        participant.lookup(factory, SHARD_A, transaction_id).state
        is participant.ParticipantState.PREPARED
    )
    with pytest.raises(participant.ParticipantConflict):
        participant.commit(factory, SHARD_A, transaction_id)
    assert (
        participant.abort(factory, SHARD_A, transaction_id).state
        is participant.ParticipantState.ABORTED
    )
    assert _balance(factory, account_id) == 20
    assert _prepared_count(factory, transaction_id) == 0


def test_abort_before_prepare_prevents_delayed_prepare(participant_database) -> None:
    _, factory = participant_database
    transaction_id = uuid4()

    aborted = participant.abort(factory, SHARD_A, transaction_id)
    delayed = participant.prepare(
        factory,
        SHARD_A,
        transaction_id,
        1,
        participant.Direction.DEBIT,
        1,
    )

    assert aborted.state is participant.ParticipantState.ABORTED
    assert delayed.state is participant.ParticipantState.ABORTED
    with pytest.raises(participant.ParticipantConflict):
        participant.commit(factory, SHARD_A, transaction_id)
