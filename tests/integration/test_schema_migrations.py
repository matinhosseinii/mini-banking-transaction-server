"""PostgreSQL integration tests for the initial database schema."""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from server.app.models import Account, Request

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPOSITORY_ROOT / "server" / "alembic.ini"
PAYLOAD_HASH = "a" * 64
BIGINT_MAX = 2**63 - 1


@contextmanager
def _temporary_database(admin_url: str) -> Generator[str, None, None]:
    database_name = f"banking_schema_test_{uuid4().hex}"
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
def migrated_database() -> Iterator[tuple[Engine, Config]]:
    admin_url = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if admin_url is None:
        pytest.skip("TEST_POSTGRES_ADMIN_URL is required for isolated PostgreSQL tests")

    with _temporary_database(admin_url) as database_url:
        alembic_config = Config(str(ALEMBIC_INI))
        alembic_config.attributes["database_url"] = database_url
        command.upgrade(alembic_config, "head")
        engine = create_engine(database_url)
        try:
            yield engine, alembic_config
        finally:
            engine.dispose()
            command.downgrade(alembic_config, "base")


def _insert_account(
    engine: Engine,
    *,
    name: str = "Test Account",
    balance: int = 0,
    status: str = "ACTIVE",
    closed_at: datetime | None = None,
) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            insert(Account).values(
                name=name,
                balance=balance,
                status=status,
                closed_at=closed_at,
            )
        )
        return result.inserted_primary_key[0]


def _assert_rejected(engine: Engine, statement: object) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(statement)


def _request_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "request_id": uuid4(),
        "requested_account_id": None,
        "account_id": None,
        "operation": "CREATE_ACCOUNT",
        "amount": None,
        "payload_hash": PAYLOAD_HASH,
        "status": "SUCCEEDED",
        "balance_before": None,
        "balance_after": 0,
        "error_code": None,
    }
    values.update(overrides)
    return values


def test_upgrade_creates_exactly_two_application_tables(migrated_database) -> None:
    engine, _ = migrated_database

    application_tables = set(inspect(engine).get_table_names()) - {"alembic_version"}

    assert application_tables == {"accounts", "requests"}


def test_downgrade_removes_both_application_tables(migrated_database) -> None:
    engine, alembic_config = migrated_database
    engine.dispose()
    command.downgrade(alembic_config, "base")
    try:
        assert "accounts" not in inspect(engine).get_table_names()
        assert "requests" not in inspect(engine).get_table_names()
    finally:
        command.upgrade(alembic_config, "head")


def test_account_rejects_negative_balance(migrated_database) -> None:
    engine, _ = migrated_database
    _assert_rejected(
        engine,
        insert(Account).values(name="Negative", balance=-1),
    )


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_account_rejects_blank_name(migrated_database, name) -> None:
    engine, _ = migrated_database
    _assert_rejected(engine, insert(Account).values(name=name))


@pytest.mark.parametrize(
    ("status", "closed_at"),
    [
        ("ACTIVE", datetime.now(UTC)),
        ("CLOSED", None),
        ("UNKNOWN", None),
    ],
)
def test_account_rejects_invalid_status_closed_at_combinations(
    migrated_database,
    status,
    closed_at,
) -> None:
    engine, _ = migrated_database
    _assert_rejected(
        engine,
        insert(Account).values(
            name="Invalid lifecycle",
            status=status,
            closed_at=closed_at,
        ),
    )


def test_closed_account_rejects_nonzero_balance(migrated_database) -> None:
    engine, _ = migrated_database
    _assert_rejected(
        engine,
        insert(Account).values(
            name="Closed with funds",
            balance=1,
            status="CLOSED",
            closed_at=datetime.now(UTC),
        ),
    )


def test_account_names_may_be_duplicated(migrated_database) -> None:
    engine, _ = migrated_database
    first_id = _insert_account(engine, name="Duplicate")
    second_id = _insert_account(engine, name="Duplicate")

    assert first_id != second_id


def test_request_ids_are_unique(migrated_database) -> None:
    engine, _ = migrated_database
    account_id = _insert_account(engine, name="Request uniqueness")
    request_id = uuid4()
    statement = insert(Request).values(
        **_request_values(request_id=request_id, account_id=account_id)
    )

    with engine.begin() as connection:
        connection.execute(statement)
    _assert_rejected(engine, statement)


@pytest.mark.parametrize(
    "values",
    [
        {"operation": "TRANSFER"},
        {"operation": "CREATE_ACCOUNT", "requested_account_id": 1},
        {"operation": "CLOSE_ACCOUNT", "requested_account_id": 1, "amount": 1},
        {
            "operation": "DEPOSIT",
            "requested_account_id": 1,
            "amount": None,
        },
        {
            "operation": "WITHDRAW",
            "requested_account_id": 1,
            "amount": 0,
        },
    ],
)
def test_request_rejects_invalid_operation_amount_combinations(
    migrated_database,
    values,
) -> None:
    engine, _ = migrated_database
    _assert_rejected(engine, insert(Request).values(**_request_values(**values)))


@pytest.mark.parametrize("payload_hash", ["", "a" * 63, "g" * 64, None])
def test_request_rejects_invalid_payload_hash(
    migrated_database,
    payload_hash,
) -> None:
    engine, _ = migrated_database
    _assert_rejected(
        engine,
        insert(Request).values(**_request_values(payload_hash=payload_hash)),
    )


@pytest.mark.parametrize(
    "values",
    [
        {"operation": "CREATE_ACCOUNT", "balance_after": None},
        {"operation": "CREATE_ACCOUNT", "balance_before": 0},
        {
            "operation": "DEPOSIT",
            "requested_account_id": 1,
            "account_id": 1,
            "amount": 10,
            "balance_before": 0,
            "balance_after": 9,
        },
        {
            "operation": "CLOSE_ACCOUNT",
            "requested_account_id": 1,
            "account_id": 1,
            "balance_before": 0,
            "balance_after": 0,
            "error_code": "ACCOUNT_ALREADY_CLOSED",
        },
    ],
)
def test_request_rejects_invalid_successful_results(
    migrated_database,
    values,
) -> None:
    engine, _ = migrated_database
    if values.get("account_id") is not None:
        account_id = _insert_account(engine, name=f"Invalid result {uuid4()}")
        values = {
            **values,
            "requested_account_id": account_id,
            "account_id": account_id,
        }
    _assert_rejected(engine, insert(Request).values(**_request_values(**values)))


@pytest.mark.parametrize(
    "values",
    [
        {
            "operation": "DEPOSIT",
            "requested_account_id": 99,
            "amount": 1,
            "status": "REJECTED",
            "balance_after": None,
            "error_code": None,
        },
        {
            "operation": "CREATE_ACCOUNT",
            "status": "REJECTED",
            "balance_after": None,
            "error_code": "ACCOUNT_NOT_FOUND",
        },
        {
            "operation": "DEPOSIT",
            "requested_account_id": 99,
            "amount": 1,
            "status": "REJECTED",
            "balance_after": None,
            "error_code": "UNKNOWN",
        },
        {
            "operation": "DEPOSIT",
            "requested_account_id": 99,
            "amount": 10,
            "status": "REJECTED",
            "balance_before": 1,
            "balance_after": 1,
            "error_code": "INSUFFICIENT_FUNDS",
        },
    ],
)
def test_request_rejects_invalid_rejected_results(
    migrated_database,
    values,
) -> None:
    engine, _ = migrated_database
    _assert_rejected(engine, insert(Request).values(**_request_values(**values)))


def test_account_not_found_allows_null_account_id(migrated_database) -> None:
    engine, _ = migrated_database
    with engine.begin() as connection:
        connection.execute(
            insert(Request).values(
                **_request_values(
                    operation="DEPOSIT",
                    requested_account_id=999999,
                    amount=10,
                    status="REJECTED",
                    balance_after=None,
                    error_code="ACCOUNT_NOT_FOUND",
                )
            )
        )


def test_non_null_account_reference_enforces_foreign_key(migrated_database) -> None:
    engine, _ = migrated_database
    _assert_rejected(
        engine,
        insert(Request).values(
            **_request_values(
                operation="DEPOSIT",
                requested_account_id=999998,
                account_id=999998,
                amount=1,
                balance_before=0,
                balance_after=1,
            )
        ),
    )


def test_non_null_account_reference_must_match_requested_account(
    migrated_database,
) -> None:
    engine, _ = migrated_database
    first_account_id = _insert_account(engine, name="Requested account")
    second_account_id = _insert_account(engine, name="Different account")
    _assert_rejected(
        engine,
        insert(Request).values(
            **_request_values(
                operation="DEPOSIT",
                requested_account_id=first_account_id,
                account_id=second_account_id,
                amount=1,
                balance_before=0,
                balance_after=1,
            )
        ),
    )


def test_valid_record_for_each_supported_operation(migrated_database) -> None:
    engine, _ = migrated_database
    active_account_id = _insert_account(engine, name="Active operations")
    closed_account_id = _insert_account(
        engine,
        name="Closed operation",
        status="CLOSED",
        closed_at=datetime.now(UTC),
    )

    records = [
        _request_values(account_id=active_account_id),
        _request_values(
            operation="CLOSE_ACCOUNT",
            requested_account_id=closed_account_id,
            account_id=closed_account_id,
            balance_before=0,
        ),
        _request_values(
            operation="DEPOSIT",
            requested_account_id=active_account_id,
            account_id=active_account_id,
            amount=5,
            balance_before=10,
            balance_after=15,
        ),
        _request_values(
            operation="WITHDRAW",
            requested_account_id=active_account_id,
            account_id=active_account_id,
            amount=5,
            balance_before=10,
            balance_after=5,
        ),
    ]

    with engine.begin() as connection:
        connection.execute(insert(Request), records)


def test_valid_rejected_business_results(migrated_database) -> None:
    engine, _ = migrated_database
    account_id = _insert_account(engine, name="Rejected results")
    records = [
        _request_values(
            operation="DEPOSIT",
            requested_account_id=account_id,
            account_id=account_id,
            amount=5,
            status="REJECTED",
            balance_before=0,
            balance_after=0,
            error_code="ACCOUNT_ALREADY_CLOSED",
        ),
        _request_values(
            operation="WITHDRAW",
            requested_account_id=account_id,
            account_id=account_id,
            amount=5,
            status="REJECTED",
            balance_before=2,
            balance_after=2,
            error_code="INSUFFICIENT_FUNDS",
        ),
        _request_values(
            operation="CLOSE_ACCOUNT",
            requested_account_id=account_id,
            account_id=account_id,
            status="REJECTED",
            balance_before=3,
            balance_after=3,
            error_code="NON_ZERO_BALANCE",
        ),
    ]

    with engine.begin() as connection:
        connection.execute(insert(Request), records)


def test_monetary_checks_are_overflow_safe(migrated_database) -> None:
    engine, _ = migrated_database
    account_id = _insert_account(engine, name="Bigint boundary")

    with engine.begin() as connection:
        connection.execute(
            insert(Request).values(
                **_request_values(
                    operation="DEPOSIT",
                    requested_account_id=account_id,
                    account_id=account_id,
                    amount=1,
                    balance_before=BIGINT_MAX - 1,
                    balance_after=BIGINT_MAX,
                )
            )
        )

    _assert_rejected(
        engine,
        insert(Request).values(
            **_request_values(
                operation="DEPOSIT",
                requested_account_id=account_id,
                account_id=account_id,
                amount=1,
                balance_before=BIGINT_MAX,
                balance_after=0,
            )
        ),
    )
