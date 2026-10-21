"""PostgreSQL integration tests for durable idempotency coordination."""

from __future__ import annotations

import os
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from server.app import idempotency
from server.app.banking import (
    AccountStatus,
    BusinessErrorCode,
    OperationStatus,
    get_balance,
)
from server.app.idempotency import (
    CoordinatedResult,
    IdempotencyConflict,
    Operation,
    coordinate_close_account,
    coordinate_create_account,
    coordinate_deposit,
    coordinate_withdraw,
    lookup_request,
)
from server.app.models import Account, Request

ALEMBIC_INI = "server/alembic.ini"


@contextmanager
def _temporary_database(admin_url: str) -> Generator[str, None, None]:
    database_name = f"idempotency_test_{uuid4().hex}"
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
def idempotency_database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    admin_url = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if admin_url is None:
        pytest.skip("TEST_POSTGRES_ADMIN_URL is required for idempotency tests")

    with _temporary_database(admin_url) as database_url:
        alembic_config = Config(ALEMBIC_INI)
        alembic_config.attributes["database_url"] = database_url
        command.upgrade(alembic_config, "head")
        engine = create_engine(
            database_url,
            pool_size=10,
            max_overflow=5,
            connect_args={"options": "-c lock_timeout=5000 -c statement_timeout=10000"},
        )
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            yield engine, session_factory
        finally:
            engine.dispose()
            command.downgrade(alembic_config, "base")


def _create_account(
    session_factory: sessionmaker[Session],
    name: str,
) -> tuple[int, UUID]:
    request_id = uuid4()
    outcome = coordinate_create_account(session_factory, request_id, name)
    assert outcome.result.account_id is not None
    return outcome.result.account_id, request_id


def _balance(session_factory: sessionmaker[Session], account_id: int) -> int:
    with session_factory() as session:
        result = get_balance(session, account_id)
    assert result.balance is not None
    return result.balance


def _run_concurrently(
    calls: list[Callable[[], object]],
) -> list[object]:
    barrier = Barrier(len(calls))

    def run(call: Callable[[], object]) -> object:
        barrier.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        futures = [executor.submit(run, call) for call in calls]
        return [future.result(timeout=15) for future in futures]


def test_successful_create_account_is_persisted(idempotency_database) -> None:
    _, session_factory = idempotency_database
    request_id = uuid4()

    outcome = coordinate_create_account(session_factory, request_id, "Persisted")
    lookup = lookup_request(session_factory, request_id)

    assert outcome.replayed is False
    assert outcome.result.operation is Operation.CREATE_ACCOUNT
    assert outcome.result.status is OperationStatus.SUCCEEDED
    assert outcome.result.requested_account_id is None
    assert outcome.result.account_id is not None
    assert outcome.result.balance_before is None
    assert outcome.result.balance_after == 0
    assert lookup.found is True
    assert lookup.result == outcome.result


def test_repeated_create_account_creates_one_account(idempotency_database) -> None:
    _, session_factory = idempotency_database
    request_id = uuid4()
    name = f"One account {uuid4()}"

    first = coordinate_create_account(session_factory, request_id, name)
    second = coordinate_create_account(session_factory, request_id, name)

    assert first.replayed is False
    assert second.replayed is True
    assert second.result == first.result
    with session_factory() as session:
        count = session.scalar(
            select(func.count()).select_from(Account).where(Account.name == name)
        )
    assert count == 1


def test_same_name_with_different_uuid_creates_another_account(
    idempotency_database,
) -> None:
    _, session_factory = idempotency_database
    name = f"Duplicate name {uuid4()}"

    first = coordinate_create_account(session_factory, uuid4(), name)
    second = coordinate_create_account(session_factory, uuid4(), name)

    assert first.result.account_id != second.result.account_id


def test_successful_deposit_and_withdraw_are_persisted(
    idempotency_database,
) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Mutations {uuid4()}")
    deposit_id = uuid4()
    withdraw_id = uuid4()

    deposited = coordinate_deposit(session_factory, deposit_id, account_id, 100)
    withdrawn = coordinate_withdraw(session_factory, withdraw_id, account_id, 40)

    assert (deposited.result.balance_before, deposited.result.balance_after) == (
        0,
        100,
    )
    assert (withdrawn.result.balance_before, withdrawn.result.balance_after) == (
        100,
        60,
    )
    assert lookup_request(session_factory, deposit_id).result == deposited.result
    assert lookup_request(session_factory, withdraw_id).result == withdrawn.result


def test_repeated_deposit_changes_balance_once(idempotency_database) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Deposit replay {uuid4()}")
    request_id = uuid4()

    first = coordinate_deposit(session_factory, request_id, account_id, 25)
    second = coordinate_deposit(session_factory, request_id, account_id, 25)

    assert second.replayed is True
    assert second.result == first.result
    assert _balance(session_factory, account_id) == 25


def test_repeated_rejected_withdrawal_returns_original_result(
    idempotency_database,
) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Rejected replay {uuid4()}")
    request_id = uuid4()

    first = coordinate_withdraw(session_factory, request_id, account_id, 10)
    coordinate_deposit(session_factory, uuid4(), account_id, 100)
    second = coordinate_withdraw(session_factory, request_id, account_id, 10)

    assert first.result.error_code is BusinessErrorCode.INSUFFICIENT_FUNDS
    assert (first.result.balance_before, first.result.balance_after) == (0, 0)
    assert second.replayed is True
    assert second.result == first.result
    assert _balance(session_factory, account_id) == 100


def test_same_uuid_with_different_amount_conflicts(idempotency_database) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Amount conflict {uuid4()}")
    request_id = uuid4()
    coordinate_deposit(session_factory, request_id, account_id, 5)

    with pytest.raises(IdempotencyConflict):
        coordinate_deposit(session_factory, request_id, account_id, 6)

    assert _balance(session_factory, account_id) == 5


def test_same_uuid_with_different_operation_conflicts(idempotency_database) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Operation conflict {uuid4()}")
    request_id = uuid4()
    coordinate_deposit(session_factory, request_id, account_id, 5)

    with pytest.raises(IdempotencyConflict):
        coordinate_withdraw(session_factory, request_id, account_id, 5)


def test_same_uuid_with_different_account_conflicts(idempotency_database) -> None:
    _, session_factory = idempotency_database
    first_id, _ = _create_account(session_factory, f"First target {uuid4()}")
    second_id, _ = _create_account(session_factory, f"Second target {uuid4()}")
    request_id = uuid4()
    coordinate_deposit(session_factory, request_id, first_id, 5)

    with pytest.raises(IdempotencyConflict):
        coordinate_deposit(session_factory, request_id, second_id, 5)

    assert _balance(session_factory, second_id) == 0


def test_same_uuid_with_different_name_conflicts(idempotency_database) -> None:
    _, session_factory = idempotency_database
    request_id = uuid4()
    coordinate_create_account(session_factory, request_id, "Original name")

    with pytest.raises(IdempotencyConflict):
        coordinate_create_account(session_factory, request_id, "Different name")


def test_successful_close_account_is_persisted(idempotency_database) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Close {uuid4()}")
    request_id = uuid4()

    closed = coordinate_close_account(session_factory, request_id, account_id)

    assert closed.result.operation is Operation.CLOSE_ACCOUNT
    assert closed.result.status is OperationStatus.SUCCEEDED
    assert (closed.result.balance_before, closed.result.balance_after) == (0, 0)
    assert lookup_request(session_factory, request_id).result == closed.result


def test_retry_after_account_closure_returns_original_result(
    idempotency_database,
) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Replay after close {uuid4()}")
    deposit_id = uuid4()
    deposited = coordinate_deposit(session_factory, deposit_id, account_id, 10)
    coordinate_withdraw(session_factory, uuid4(), account_id, 10)
    coordinate_close_account(session_factory, uuid4(), account_id)

    replayed = coordinate_deposit(session_factory, deposit_id, account_id, 10)

    assert replayed.replayed is True
    assert replayed.result == deposited.result
    with session_factory() as session:
        balance = get_balance(session, account_id)
    assert (balance.balance, balance.status) == (0, AccountStatus.CLOSED)


def test_request_tracking_returns_persisted_outcome(idempotency_database) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Tracking {uuid4()}")
    request_id = uuid4()
    persisted = coordinate_deposit(session_factory, request_id, account_id, 7)

    tracked = lookup_request(session_factory, request_id)

    assert tracked.found is True
    assert tracked.result == persisted.result


def test_unknown_request_tracking_returns_not_found(idempotency_database) -> None:
    _, session_factory = idempotency_database

    tracked = lookup_request(session_factory, uuid4())

    assert tracked.found is False
    assert tracked.result is None


def test_failure_rolls_back_effect_and_request_then_retry_succeeds(
    idempotency_database,
    monkeypatch,
) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Rollback retry {uuid4()}")
    request_id = uuid4()
    original_deposit = idempotency.banking.deposit

    def fail_after_deposit(session: Session, target_id: int, amount: int):
        original_deposit(session, target_id, amount)
        raise RuntimeError("injected failure before request persistence")

    monkeypatch.setattr(idempotency.banking, "deposit", fail_after_deposit)
    with pytest.raises(RuntimeError):
        coordinate_deposit(session_factory, request_id, account_id, 20)

    assert _balance(session_factory, account_id) == 0
    assert lookup_request(session_factory, request_id).found is False

    monkeypatch.setattr(idempotency.banking, "deposit", original_deposit)
    retried = coordinate_deposit(session_factory, request_id, account_id, 20)
    assert retried.replayed is False
    assert _balance(session_factory, account_id) == 20


def test_concurrent_same_uuid_same_payload_has_one_effect(
    idempotency_database,
) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Concurrent replay {uuid4()}")
    request_id = uuid4()

    results = _run_concurrently(
        [
            lambda: coordinate_deposit(session_factory, request_id, account_id, 11),
            lambda: coordinate_deposit(session_factory, request_id, account_id, 11),
        ]
    )

    outcomes = [result for result in results if isinstance(result, CoordinatedResult)]
    assert len(outcomes) == 2
    assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
    assert outcomes[0].result == outcomes[1].result
    assert _balance(session_factory, account_id) == 11


def test_concurrent_same_uuid_different_payload_has_one_conflict(
    idempotency_database,
) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Concurrent conflict {uuid4()}")
    request_id = uuid4()

    def attempt(amount: int) -> object:
        try:
            return coordinate_deposit(session_factory, request_id, account_id, amount)
        except IdempotencyConflict as error:
            return error

    results = _run_concurrently([lambda: attempt(5), lambda: attempt(7)])

    assert sum(isinstance(result, CoordinatedResult) for result in results) == 1
    assert sum(isinstance(result, IdempotencyConflict) for result in results) == 1
    assert _balance(session_factory, account_id) in {5, 7}


def test_concurrent_create_account_same_uuid_creates_one_account(
    idempotency_database,
) -> None:
    _, session_factory = idempotency_database
    request_id = uuid4()
    name = f"Concurrent account {uuid4()}"

    results = _run_concurrently(
        [
            lambda: coordinate_create_account(session_factory, request_id, name),
            lambda: coordinate_create_account(session_factory, request_id, name),
        ]
    )

    outcomes = [result for result in results if isinstance(result, CoordinatedResult)]
    assert len(outcomes) == 2
    assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
    with session_factory() as session:
        count = session.scalar(
            select(func.count()).select_from(Account).where(Account.name == name)
        )
    assert count == 1


def test_different_uuids_on_same_account_remain_concurrency_safe(
    idempotency_database,
) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Independent UUIDs {uuid4()}")

    results = _run_concurrently(
        [
            lambda: coordinate_deposit(session_factory, uuid4(), account_id, 10),
            lambda: coordinate_deposit(session_factory, uuid4(), account_id, 20),
        ]
    )

    outcomes = [result for result in results if isinstance(result, CoordinatedResult)]
    assert len(outcomes) == 2
    assert all(outcome.replayed is False for outcome in outcomes)
    assert _balance(session_factory, account_id) == 30


def test_coordinator_persists_only_final_request_rows(idempotency_database) -> None:
    _, session_factory = idempotency_database
    account_id, _ = _create_account(session_factory, f"Final statuses {uuid4()}")
    coordinate_withdraw(session_factory, uuid4(), account_id, 1)

    with session_factory() as session:
        statuses = set(session.scalars(select(Request.status)))

    assert statuses == {OperationStatus.SUCCEEDED, OperationStatus.REJECTED}
