"""PostgreSQL integration tests for transactional Banking Service behavior."""

from __future__ import annotations

import os
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from server.app.banking import (
    AccountStatus,
    BusinessErrorCode,
    MutationResult,
    OperationStatus,
    close_account,
    create_account,
    deposit,
    get_balance,
    withdraw,
)
from server.app.models import Account, Request

ALEMBIC_INI = "server/alembic.ini"
BIGINT_MAX = 2**63 - 1


@contextmanager
def _temporary_database(admin_url: str) -> Generator[str, None, None]:
    database_name = f"banking_service_test_{uuid4().hex}"
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
def banking_engine() -> Iterator[Engine]:
    admin_url = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if admin_url is None:
        pytest.skip("TEST_POSTGRES_ADMIN_URL is required for Banking Service tests")

    with _temporary_database(admin_url) as database_url:
        alembic_config = Config(ALEMBIC_INI)
        alembic_config.attributes["database_url"] = database_url
        command.upgrade(alembic_config, "head")
        engine = create_engine(database_url, pool_size=10, max_overflow=5)
        try:
            yield engine
        finally:
            engine.dispose()
            command.downgrade(alembic_config, "base")


def _sessions(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def _create_committed_account(engine: Engine, name: str) -> int:
    with _sessions(engine).begin() as session:
        result = create_account(session, name)
    assert result.account_id is not None
    return result.account_id


def _deposit_committed(engine: Engine, account_id: int, amount: int) -> None:
    with _sessions(engine).begin() as session:
        result = deposit(session, account_id, amount)
    assert result.status is OperationStatus.SUCCEEDED


def _run_concurrently(
    engine: Engine,
    operations: list[Callable[[Session], MutationResult]],
) -> list[MutationResult]:
    barrier = Barrier(len(operations))
    session_factory = _sessions(engine)

    def run(operation: Callable[[Session], MutationResult]) -> MutationResult:
        with session_factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            barrier.wait(timeout=5)
            return operation(session)

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(run, operation) for operation in operations]
        return [future.result(timeout=15) for future in futures]


def test_create_account_success_and_duplicate_names(banking_engine) -> None:
    with _sessions(banking_engine).begin() as session:
        first = create_account(session, "Same Name")
        second = create_account(session, "Same Name")

    assert first.status is OperationStatus.SUCCEEDED
    assert first.balance_before is None
    assert first.balance_after == 0
    assert first.account_id is not None
    assert second.account_id is not None
    assert first.account_id != second.account_id


def test_account_creation_rolls_back_with_caller_transaction(banking_engine) -> None:
    session_factory = _sessions(banking_engine)
    with session_factory() as session:
        transaction = session.begin()
        result = create_account(session, "Rolled Back Account")
        transaction.rollback()

    assert result.account_id is not None
    with session_factory() as session:
        lookup = get_balance(session, result.account_id)
    assert lookup.error_code is BusinessErrorCode.ACCOUNT_NOT_FOUND


def test_successful_deposit_and_withdraw(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Deposit and withdraw")
    with _sessions(banking_engine).begin() as session:
        deposited = deposit(session, account_id, 100)
        withdrawn = withdraw(session, account_id, 40)

    assert (deposited.balance_before, deposited.balance_after) == (0, 100)
    assert deposited.status is OperationStatus.SUCCEEDED
    assert (withdrawn.balance_before, withdrawn.balance_after) == (100, 60)
    assert withdrawn.status is OperationStatus.SUCCEEDED


def test_insufficient_funds_preserves_balance(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Insufficient funds")
    _deposit_committed(banking_engine, account_id, 5)

    with _sessions(banking_engine).begin() as session:
        result = withdraw(session, account_id, 6)

    assert result.status is OperationStatus.REJECTED
    assert result.error_code is BusinessErrorCode.INSUFFICIENT_FUNDS
    assert (result.balance_before, result.balance_after) == (5, 5)


def test_account_not_found_results(banking_engine) -> None:
    missing_id = BIGINT_MAX
    with _sessions(banking_engine).begin() as session:
        results = [
            close_account(session, missing_id),
            deposit(session, missing_id, 1),
            withdraw(session, missing_id, 1),
        ]
        lookup = get_balance(session, missing_id)

    assert all(result.account_id is None for result in results)
    assert all(result.balance_before is None for result in results)
    assert all(result.balance_after is None for result in results)
    assert all(
        result.error_code is BusinessErrorCode.ACCOUNT_NOT_FOUND for result in results
    )
    assert lookup.error_code is BusinessErrorCode.ACCOUNT_NOT_FOUND


def test_successful_account_closure(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Close successfully")
    with _sessions(banking_engine).begin() as session:
        result = close_account(session, account_id)

    assert result.status is OperationStatus.SUCCEEDED
    assert (result.balance_before, result.balance_after) == (0, 0)
    with _sessions(banking_engine)() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.status == AccountStatus.CLOSED
        assert account.closed_at is not None


def test_closure_with_nonzero_balance_is_rejected(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Nonzero closure")
    _deposit_committed(banking_engine, account_id, 10)

    with _sessions(banking_engine).begin() as session:
        result = close_account(session, account_id)

    assert result.error_code is BusinessErrorCode.NON_ZERO_BALANCE
    assert (result.balance_before, result.balance_after) == (10, 10)


def test_repeated_closure_is_rejected(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Repeated closure")
    with _sessions(banking_engine).begin() as session:
        assert close_account(session, account_id).status is OperationStatus.SUCCEEDED
    with _sessions(banking_engine).begin() as session:
        repeated = close_account(session, account_id)

    assert repeated.error_code is BusinessErrorCode.ACCOUNT_ALREADY_CLOSED
    assert (repeated.balance_before, repeated.balance_after) == (0, 0)


def test_financial_operations_reject_closed_account(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Closed operations")
    with _sessions(banking_engine).begin() as session:
        close_account(session, account_id)
    with _sessions(banking_engine).begin() as session:
        deposited = deposit(session, account_id, 1)
        withdrawn = withdraw(session, account_id, 1)

    assert deposited.error_code is BusinessErrorCode.ACCOUNT_ALREADY_CLOSED
    assert withdrawn.error_code is BusinessErrorCode.ACCOUNT_ALREADY_CLOSED
    assert (deposited.balance_before, deposited.balance_after) == (0, 0)
    assert (withdrawn.balance_before, withdrawn.balance_after) == (0, 0)


def test_balance_lookup_supports_active_and_closed_accounts(banking_engine) -> None:
    active_id = _create_committed_account(banking_engine, "Active lookup")
    closed_id = _create_committed_account(banking_engine, "Closed lookup")
    _deposit_committed(banking_engine, active_id, 7)
    with _sessions(banking_engine).begin() as session:
        close_account(session, closed_id)

    with _sessions(banking_engine)() as session:
        active = get_balance(session, active_id)
        closed = get_balance(session, closed_id)

    assert (active.account_id, active.balance, active.status) == (
        active_id,
        7,
        AccountStatus.ACTIVE,
    )
    assert (closed.account_id, closed.balance, closed.status) == (
        closed_id,
        0,
        AccountStatus.CLOSED,
    )


def test_balance_modification_rolls_back_with_caller_transaction(
    banking_engine,
) -> None:
    account_id = _create_committed_account(banking_engine, "Rollback balance")
    session_factory = _sessions(banking_engine)
    with session_factory() as session:
        transaction = session.begin()
        assert deposit(session, account_id, 25).balance_after == 25
        transaction.rollback()

    with session_factory() as session:
        assert get_balance(session, account_id).balance == 0


def test_database_overflow_propagates_to_caller(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Overflow")
    _deposit_committed(banking_engine, account_id, BIGINT_MAX)

    with pytest.raises(DBAPIError), _sessions(banking_engine).begin() as session:
        deposit(session, account_id, 1)

    with _sessions(banking_engine)() as session:
        assert get_balance(session, account_id).balance == BIGINT_MAX


def test_concurrent_deposits_do_not_lose_updates(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Concurrent deposits")
    operations = [lambda session: deposit(session, account_id, 10) for _ in range(8)]

    results = _run_concurrently(banking_engine, operations)

    assert all(result.status is OperationStatus.SUCCEEDED for result in results)
    assert sorted(result.balance_before for result in results) == list(range(0, 80, 10))
    with _sessions(banking_engine)() as session:
        assert get_balance(session, account_id).balance == 80


def test_concurrent_withdrawals_never_make_balance_negative(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Concurrent withdrawals")
    _deposit_committed(banking_engine, account_id, 100)
    operations = [lambda session: withdraw(session, account_id, 15) for _ in range(10)]

    results = _run_concurrently(banking_engine, operations)

    succeeded = [
        result for result in results if result.status is OperationStatus.SUCCEEDED
    ]
    rejected = [
        result for result in results if result.status is OperationStatus.REJECTED
    ]
    assert len(succeeded) == 6
    assert len(rejected) == 4
    assert all(
        result.error_code is BusinessErrorCode.INSUFFICIENT_FUNDS for result in rejected
    )
    with _sessions(banking_engine)() as session:
        assert get_balance(session, account_id).balance == 10


def test_concurrent_deposit_and_closure_are_consistent(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Deposit versus close")
    deposited, closed = _run_concurrently(
        banking_engine,
        [
            lambda session: deposit(session, account_id, 10),
            lambda session: close_account(session, account_id),
        ],
    )

    with _sessions(banking_engine)() as session:
        final = get_balance(session, account_id)
    if deposited.status is OperationStatus.SUCCEEDED:
        assert closed.error_code is BusinessErrorCode.NON_ZERO_BALANCE
        assert (final.balance, final.status) == (10, AccountStatus.ACTIVE)
    else:
        assert deposited.error_code is BusinessErrorCode.ACCOUNT_ALREADY_CLOSED
        assert closed.status is OperationStatus.SUCCEEDED
        assert (final.balance, final.status) == (0, AccountStatus.CLOSED)


def test_concurrent_withdrawal_and_closure_are_consistent(banking_engine) -> None:
    account_id = _create_committed_account(banking_engine, "Withdraw versus close")
    _deposit_committed(banking_engine, account_id, 10)
    withdrawn, closed = _run_concurrently(
        banking_engine,
        [
            lambda session: withdraw(session, account_id, 10),
            lambda session: close_account(session, account_id),
        ],
    )

    assert withdrawn.status is OperationStatus.SUCCEEDED
    with _sessions(banking_engine)() as session:
        final = get_balance(session, account_id)
    if closed.status is OperationStatus.SUCCEEDED:
        assert (final.balance, final.status) == (0, AccountStatus.CLOSED)
    else:
        assert closed.error_code is BusinessErrorCode.NON_ZERO_BALANCE
        assert (final.balance, final.status) == (0, AccountStatus.ACTIVE)


def test_banking_service_does_not_create_request_records(banking_engine) -> None:
    with _sessions(banking_engine)() as session:
        request_count = session.scalar(select(func.count()).select_from(Request))

    assert request_count == 0
