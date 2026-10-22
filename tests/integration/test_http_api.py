"""End-to-end HTTP tests against an isolated PostgreSQL database."""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from server.app.main import app, get_session_factory
from server.app.models import Account, Request


@contextmanager
def _temporary_database(admin_url: str) -> Generator[str, None, None]:
    database_name = f"banking_http_test_{uuid4().hex}"
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
def http_database() -> Iterator[sessionmaker[Session]]:
    admin_url = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if admin_url is None:
        pytest.skip("TEST_POSTGRES_ADMIN_URL is required for HTTP integration tests")

    with _temporary_database(admin_url) as database_url:
        config = Config("server/alembic.ini")
        config.attributes["database_url"] = database_url
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        try:
            yield sessionmaker(bind=engine, expire_on_commit=False)
        finally:
            engine.dispose()
            command.downgrade(config, "base")


@pytest.fixture
def client(http_database) -> Iterator[TestClient]:
    app.dependency_overrides[get_session_factory] = lambda: http_database
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()


def _mutate(client: TestClient, method: str, path: str, body=None, key=None):
    return client.request(
        method,
        path,
        json=body,
        headers={"Idempotency-Key": str(key or uuid4())},
    )


def test_full_account_lifecycle_and_request_lookup(client, http_database) -> None:
    create_id = uuid4()
    created = _mutate(
        client, "POST", "/accounts", {"name": "HTTP lifecycle"}, create_id
    )
    assert created.status_code == 201
    account_id = created.json()["account_id"]

    replay = _mutate(client, "POST", "/accounts", {"name": "HTTP lifecycle"}, create_id)
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["account_id"] == account_id

    deposit_id = uuid4()
    deposited = _mutate(
        client,
        "POST",
        f"/accounts/{account_id}/deposit",
        {"amount": 500},
        deposit_id,
    )
    assert deposited.status_code == 200
    assert deposited.json()["balance_after"] == 500

    replayed_deposit = _mutate(
        client,
        "POST",
        f"/accounts/{account_id}/deposit",
        {"amount": 500},
        deposit_id,
    )
    assert replayed_deposit.json()["replayed"] is True

    balance = client.get(f"/accounts/{account_id}/balance")
    assert balance.json() == {
        "account_id": account_id,
        "balance": 500,
        "status": "ACTIVE",
    }

    withdrawn = _mutate(
        client,
        "POST",
        f"/accounts/{account_id}/withdraw",
        {"amount": 500},
    )
    assert withdrawn.status_code == 200
    closed = _mutate(client, "DELETE", f"/accounts/{account_id}")
    assert closed.status_code == 200
    assert client.get(f"/accounts/{account_id}/balance").json()["status"] == "CLOSED"

    lookup = client.get(f"/requests/{deposit_id}")
    assert lookup.status_code == 200
    assert lookup.json()["request_id"] == str(deposit_id)
    assert "replayed" not in lookup.json()
    assert "payload_hash" not in lookup.json()

    with http_database() as session:
        assert session.scalar(select(func.count()).select_from(Account)) == 1
        assert session.scalar(select(func.count()).select_from(Request)) == 4


def test_conflicts_and_business_rejections_are_durable(client, http_database) -> None:
    missing_id = uuid4()
    missing = _mutate(
        client, "POST", "/accounts/999999/deposit", {"amount": 10}, missing_id
    )
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "ACCOUNT_NOT_FOUND"

    replay = _mutate(
        client, "POST", "/accounts/999999/deposit", {"amount": 10}, missing_id
    )
    assert replay.status_code == 404
    assert replay.json()["replayed"] is True

    conflict = _mutate(
        client, "POST", "/accounts/999999/deposit", {"amount": 11}, missing_id
    )
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

    with http_database() as session:
        stored = session.get(Request, missing_id)
        assert stored is not None
        assert stored.status == "REJECTED"
        assert stored.amount == 10


def test_history_filters_counts_and_paginates_in_sql(client) -> None:
    created = _mutate(client, "POST", "/accounts", {"name": "History account"})
    account_id = created.json()["account_id"]
    _mutate(client, "POST", f"/accounts/{account_id}/deposit", {"amount": 3})
    _mutate(client, "POST", f"/accounts/{account_id}/withdraw", {"amount": 99})

    history = client.get(
        "/requests",
        params={
            "account_id": account_id,
            "status": "SUCCEEDED",
            "operation": "DEPOSIT",
            "limit": 1,
            "offset": 0,
        },
    )
    assert history.status_code == 200
    assert history.json()["total"] == 1
    assert len(history.json()["items"]) == 1
    assert history.json()["items"][0]["operation"] == "DEPOSIT"
    assert "payload_hash" not in history.text
    assert "replayed" not in history.text

    empty_page = client.get(
        "/requests", params={"account_id": account_id, "offset": 100}
    ).json()
    assert empty_page["items"] == []
    assert empty_page["total"] == 3


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"account_id": 0},
        {"status": "PENDING"},
        {"operation": "TRANSFER"},
    ],
)
def test_invalid_history_filters_are_problem_details(client, params) -> None:
    response = client.get("/requests", params=params)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
