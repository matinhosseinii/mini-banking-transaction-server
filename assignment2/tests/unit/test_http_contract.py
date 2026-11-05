"""Focused HTTP contract tests that do not require PostgreSQL."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

from server.app.banking import BusinessErrorCode, OperationStatus
from server.app.idempotency import CoordinatedResult, Operation, RequestResult
from server.app.main import app, get_session_factory


@pytest.fixture
def client() -> TestClient:
    app.dependency_overrides[get_session_factory] = lambda: MagicMock()
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()


def _outcome(
    *,
    operation: Operation = Operation.DEPOSIT,
    status: OperationStatus = OperationStatus.SUCCEEDED,
    error_code: BusinessErrorCode | None = None,
    replayed: bool = False,
) -> CoordinatedResult:
    account_id = None if error_code is BusinessErrorCode.ACCOUNT_NOT_FOUND else 7
    return CoordinatedResult(
        result=RequestResult(
            request_id=uuid4(),
            operation=operation,
            requested_account_id=None if operation is Operation.CREATE_ACCOUNT else 7,
            account_id=account_id,
            amount=10 if operation in {Operation.DEPOSIT, Operation.WITHDRAW} else None,
            payload_hash="a" * 64,
            status=status,
            balance_before=None if account_id is None else 0,
            balance_after=None if account_id is None else 10,
            error_code=error_code,
            created_at=datetime.now(UTC),
        ),
        replayed=replayed,
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/accounts", {"name": "Valid"}),
        ("delete", "/accounts/1", None),
        ("post", "/accounts/1/deposit", {"amount": 1}),
        ("post", "/accounts/1/withdraw", {"amount": 1}),
    ],
)
def test_mutations_require_idempotency_key(client, method, path, body) -> None:
    response = client.request(method, path, json=body)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "Request validation failed."


def test_malformed_idempotency_key_is_validation_problem(client) -> None:
    response = client.post(
        "/accounts", json={"name": "Valid"}, headers={"Idempotency-Key": "bad"}
    )

    assert response.status_code == 422
    assert response.json()["status"] == 422


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "1", None])
def test_invalid_amount_is_rejected_before_service(client, amount) -> None:
    response = client.post(
        "/accounts/1/deposit",
        json={"amount": amount},
        headers={"Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("name", ["", "   ", "x" * 101, 1, None])
def test_invalid_name_is_rejected_before_service(client, name) -> None:
    response = client.post(
        "/accounts",
        json={"name": name},
        headers={"Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 422


def test_successful_create_uses_201_and_hides_payload_hash(client, monkeypatch) -> None:
    outcome = _outcome(operation=Operation.CREATE_ACCOUNT, replayed=True)
    monkeypatch.setattr(
        "server.app.routes.idempotency.coordinate_create_account",
        lambda session_factory, request_id, name: outcome,
    )

    response = client.post(
        "/accounts",
        json={"name": "  Preserved Name  "},
        headers={"Idempotency-Key": str(outcome.result.request_id)},
    )

    assert response.status_code == 201
    assert response.json()["replayed"] is True
    assert "payload_hash" not in response.json()


def test_business_rejection_is_replayable_problem(client, monkeypatch) -> None:
    outcome = _outcome(
        status=OperationStatus.REJECTED,
        error_code=BusinessErrorCode.ACCOUNT_NOT_FOUND,
        replayed=True,
    )
    monkeypatch.setattr(
        "server.app.routes.idempotency.coordinate_deposit",
        lambda session_factory, request_id, account_id, amount: outcome,
    )

    response = client.post(
        "/accounts/7/deposit",
        json={"amount": 10},
        headers={"Idempotency-Key": str(outcome.result.request_id)},
    )

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["error_code"] == "ACCOUNT_NOT_FOUND"
    assert response.json()["replayed"] is True


def test_insufficient_funds_is_meaningful_problem(client, monkeypatch) -> None:
    outcome = _outcome(
        operation=Operation.WITHDRAW,
        status=OperationStatus.REJECTED,
        error_code=BusinessErrorCode.INSUFFICIENT_FUNDS,
    )
    monkeypatch.setattr(
        "server.app.routes.idempotency.coordinate_withdraw",
        lambda *args: outcome,
    )

    response = client.post(
        "/accounts/7/withdraw",
        json={"amount": 10},
        headers={"Idempotency-Key": str(outcome.result.request_id)},
    )

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["error_code"] == "INSUFFICIENT_FUNDS"
    assert response.json()["detail"] == "Insufficient funds."


@pytest.mark.parametrize(
    ("content", "content_type"),
    [(b"{}", "application/json"), (b'{"amount":', "application/json")],
)
def test_missing_fields_and_malformed_json_are_validation_problems(
    client, content, content_type
) -> None:
    response = client.post(
        "/accounts/7/deposit",
        content=content,
        headers={
            "Content-Type": content_type,
            "Idempotency-Key": str(uuid4()),
        },
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "Request validation failed."
    assert response.json()["errors"]


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (OperationalError("SELECT", {}, OSError("offline")), 503),
        (IntegrityError("INSERT", {}, ValueError("bad row")), 500),
        (RuntimeError("secret internal message"), 500),
    ],
)
def test_technical_errors_are_classified_and_sanitized(
    client, monkeypatch, error, expected_status
) -> None:
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("server.app.routes.idempotency.coordinate_deposit", fail)
    response = client.post(
        "/accounts/1/deposit",
        json={"amount": 1},
        headers={"Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == expected_status
    assert response.headers["content-type"] == "application/problem+json"
    assert "secret" not in response.text
    assert "SELECT" not in response.text


def test_openapi_documents_only_supported_routes_and_problem_media_type(client) -> None:
    document = client.get("/openapi.json").json()

    assert set(document["paths"]) == {
        "/ready",
        "/accounts",
        "/accounts/{account_id}",
        "/accounts/{account_id}/balance",
        "/accounts/{account_id}/deposit",
        "/accounts/{account_id}/withdraw",
        "/requests/{request_id}",
        "/requests",
    }
    create = document["paths"]["/accounts"]["post"]
    assert create["responses"]["201"]
    assert "application/problem+json" in create["responses"]["422"]["content"]
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["required"]
        for parameter in create["parameters"]
    )


@pytest.mark.parametrize(
    "imports",
    [
        "import server.app.main as main",
        "import server.app.routes; import server.app.main as main",
    ],
)
def test_import_order_registers_all_routes_in_fresh_process(imports) -> None:
    expected_paths = {
        "/ready",
        "/accounts",
        "/accounts/{account_id}",
        "/accounts/{account_id}/balance",
        "/accounts/{account_id}/deposit",
        "/accounts/{account_id}/withdraw",
        "/requests/{request_id}",
        "/requests",
    }
    script = (
        f"{imports}; "
        "paths = set(main.app.openapi()['paths']); "
        f"assert {expected_paths!r}.issubset(paths), paths"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_method_not_allowed_preserves_allow_header(client) -> None:
    response = client.put("/accounts")

    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["allow"] == "POST"
    assert response.json() == {
        "type": "about:blank",
        "title": "Method Not Allowed",
        "status": 405,
        "detail": "Method Not Allowed",
    }
