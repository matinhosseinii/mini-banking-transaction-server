"""PostgreSQL integration coverage for independent shard ownership."""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import ExitStack, contextmanager
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from server.app.banking import OperationStatus, deposit, get_balance, withdraw
from server.app.config import Settings
from server.app.database import create_session_factory
from server.app.initialize_shard import initialize_database
from server.app.models import Account
from server.app.seed import seed_accounts
from server.app.sharding import WrongShardError, get_shard


@contextmanager
def _temporary_database(admin_url: str, label: str) -> Generator[str, None, None]:
    database_name = f"banking_shard_{label}_{uuid4().hex}"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    database_url = make_url(admin_url).set(database=database_name)
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        yield database_url.render_as_string(hide_password=False)
    finally:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_engine.dispose()


@pytest.fixture(scope="module")
def shard_engines() -> Iterator[dict[str, Engine]]:
    admin_url = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if admin_url is None:
        pytest.skip("TEST_POSTGRES_ADMIN_URL is required for shard database tests")

    with ExitStack() as stack:
        urls = {
            shard_id: stack.enter_context(
                _temporary_database(admin_url, shard_id.lower())
            )
            for shard_id in ("A", "B")
        }
        engines = {}
        for shard_id, database_url in urls.items():
            alembic = Config("server/alembic.ini")
            alembic.attributes["database_url"] = database_url
            command.upgrade(alembic, "head")
            initialize_database(Settings(database_url, 100, "INFO", shard_id))
            engines[shard_id] = create_engine(database_url)
        try:
            yield engines
        finally:
            for engine in engines.values():
                engine.dispose()


def _sessions(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def test_seed_is_repeatable_and_accounts_exist_only_in_owner_database(
    shard_engines,
) -> None:
    expected = {"A": [1, 2, 3], "B": [1001, 1002, 1003]}
    for shard_id, engine in shard_engines.items():
        shard = get_shard(shard_id)
        first = seed_accounts(_sessions(engine), shard)
        second = seed_accounts(_sessions(engine), shard)
        assert [item.account_id for item in first] == expected[shard_id]
        assert all(item.create_replayed for item in second)
        assert [item.deposit_replayed for item in second] == [True, True, None]
        with _sessions(engine)() as session:
            ids = list(session.scalars(select(Account.id).order_by(Account.id)))
            assert ids == expected[shard_id]
            assert session.scalar(select(func.count()).select_from(Account)) == 3


def test_balances_survive_application_engine_restart(shard_engines) -> None:
    engine = shard_engines["B"]
    with _sessions(engine).begin() as session:
        deposited = deposit(session, 1001, 500)
        withdrawn = withdraw(session, 1001, 125)
    assert deposited.status is OperationStatus.SUCCEEDED
    assert withdrawn.balance_after == deposited.balance_after - 125

    database_url = engine.url.render_as_string(hide_password=False)
    engine.dispose()
    restarted_engine = create_engine(database_url)
    try:
        with _sessions(restarted_engine)() as session:
            persisted = get_balance(session, 1001)
        assert persisted.balance == withdrawn.balance_after
    finally:
        restarted_engine.dispose()


def test_wrong_shard_rejection_does_not_modify_balance(shard_engines) -> None:
    with _sessions(shard_engines["A"])() as session:
        before = get_balance(session, 1)
    with pytest.raises(WrongShardError):
        get_shard("B").require_owner(1)
    with _sessions(shard_engines["A"])() as session:
        after = get_balance(session, 1)
    assert after == before


def test_reinitialization_never_moves_sequence_backward(shard_engines) -> None:
    engine = shard_engines["A"]
    with engine.begin() as connection:
        connection.execute(text("SELECT setval('accounts_id_seq', 900, true)"))

    database_url = engine.url.render_as_string(hide_password=False)
    initialize_database(Settings(database_url, 100, "INFO", "A"))

    with engine.begin() as connection:
        next_id = connection.execute(
            text("SELECT nextval('accounts_id_seq')")
        ).scalar_one()
    assert next_id == 901
