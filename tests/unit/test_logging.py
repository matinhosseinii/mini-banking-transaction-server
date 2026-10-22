"""Tests for safe structured logging and HTTP correlation behavior."""

from __future__ import annotations

import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from server.app.banking import BusinessErrorCode, OperationStatus
from server.app.idempotency import CoordinatedResult, Operation, RequestResult
from server.app.logging_config import (
    JsonFormatter,
    build_logging_config,
    correlation_id_context,
)
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
    status: OperationStatus = OperationStatus.SUCCEEDED,
    error_code: BusinessErrorCode | None = None,
    replayed: bool = False,
) -> CoordinatedResult:
    return CoordinatedResult(
        result=RequestResult(
            request_id=uuid4(),
            operation=Operation.DEPOSIT,
            requested_account_id=7,
            account_id=(
                None if error_code is BusinessErrorCode.ACCOUNT_NOT_FOUND else 7
            ),
            amount=10,
            payload_hash="a" * 64,
            status=status,
            balance_before=None if error_code else 0,
            balance_after=None if error_code else 10,
            error_code=error_code,
            created_at=datetime.now(UTC),
        ),
        replayed=replayed,
    )


def _completion_records(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "http_request_completed"
    ]


def test_json_formatter_emits_safe_common_fields_without_traceback() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.safe_json")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        raise RuntimeError("sensitive exception text")
    except RuntimeError:
        logger.log(
            logging.ERROR,
            "ignored raw message",
            exc_info=True,
            extra={"event": "safe_failure", "duration_ms": object()},
        )

    document = json.loads(stream.getvalue())
    assert document["level"] == "ERROR"
    assert document["logger"] == "tests.safe_json"
    assert document["event"] == "safe_failure"
    assert document["duration_ms"] == "<object>"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*Z", document["timestamp"])
    assert "sensitive" not in stream.getvalue()
    assert "Traceback" not in stream.getvalue()


def test_uvicorn_logging_config_is_json_and_disables_access_output() -> None:
    config = build_logging_config("WARNING")

    assert config["root"]["level"] == "WARNING"
    assert config["loggers"]["uvicorn.error"]["handlers"] == ["json"]
    assert config["loggers"]["uvicorn.access"]["handlers"] == ["null"]
    assert config["formatters"]["json"]["()"].endswith("JsonFormatter")


@pytest.mark.parametrize(
    ("message", "event"),
    [
        ("Application startup complete.", "uvicorn_application_started"),
        ("Application shutdown complete.", "uvicorn_application_stopped"),
        ("Unhandled operational condition", "uvicorn_log"),
    ],
)
def test_uvicorn_lifecycle_and_error_records_use_safe_json(message, event) -> None:
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        message,
        (),
        None,
    )

    document = json.loads(JsonFormatter().format(record))

    assert document["logger"] == "uvicorn.error"
    assert document["level"] == "ERROR"
    assert document["event"] == event
    assert message not in json.dumps(document)


def test_runner_uses_json_configuration_and_disables_access_log(monkeypatch) -> None:
    from server.app import run

    uvicorn_run = MagicMock()
    monkeypatch.setattr(run.uvicorn, "run", uvicorn_run)
    monkeypatch.setenv("LOG_LEVEL", "ERROR")

    run.main()

    kwargs = uvicorn_run.call_args.kwargs
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8000
    assert kwargs["access_log"] is False
    assert kwargs["log_config"]["root"]["level"] == "ERROR"


def test_server_generated_ids_are_uuid4_distinct_and_ignore_client_value(
    client,
) -> None:
    supplied = str(uuid4())
    first = client.get("/missing", headers={"X-Request-ID": supplied})
    second = client.get("/missing")

    first_id = UUID(first.headers["X-Request-ID"])
    second_id = UUID(second.headers["X-Request-ID"])
    assert first_id.version == 4
    assert second_id.version == 4
    assert first_id != second_id
    assert str(first_id) != supplied


def test_concurrent_requests_keep_distinct_correlation_contexts(client, caplog) -> None:
    with (
        caplog.at_level(logging.INFO, logger="server.app.http"),
        ThreadPoolExecutor(max_workers=4) as executor,
    ):
        responses = list(executor.map(lambda _: client.get("/requests"), range(4)))

    response_ids = {response.headers["X-Request-ID"] for response in responses}
    log_ids = {record.correlation_id for record in _completion_records(caplog)}
    assert len(response_ids) == 4
    assert log_ids == response_ids


def test_success_completion_has_normalized_path_metadata_and_no_sensitive_data(
    client, caplog, monkeypatch
) -> None:
    outcome = _outcome(replayed=True)
    monkeypatch.setattr(
        "server.app.routes.idempotency.coordinate_deposit",
        lambda *args: outcome,
    )
    key = uuid4()

    with caplog.at_level(logging.INFO, logger="server.app.http"):
        response = client.post(
            "/accounts/7/deposit?private=query",
            json={"amount": 10},
            headers={"Idempotency-Key": str(key)},
        )

    records = _completion_records(caplog)
    assert response.status_code == 200
    assert len(records) == 1
    record = records[0]
    assert record.correlation_id == response.headers["X-Request-ID"]
    assert record.path == "/accounts/{account_id}/deposit"
    assert record.status_code == 200
    assert record.duration_ms >= 0
    assert record.idempotency_key == str(key)
    assert record.operation == "DEPOSIT"
    assert record.business_status == "SUCCEEDED"
    assert record.replayed is True
    assert not hasattr(record, "amount")
    assert "private" not in record.path


def test_synchronous_endpoint_logging_retains_correlation_context(
    client, caplog, monkeypatch
) -> None:
    service_logger = logging.getLogger("server.app.sync_test")
    outcome = _outcome()
    seen_correlation_ids = []

    def coordinated_in_worker(*args):
        seen_correlation_ids.append(correlation_id_context.get())
        service_logger.info(
            "Synchronous service called",
            extra={"event": "sync_service_called"},
        )
        return outcome

    monkeypatch.setattr(
        "server.app.routes.idempotency.coordinate_deposit", coordinated_in_worker
    )
    with caplog.at_level(logging.INFO):
        response = client.post(
            "/accounts/7/deposit",
            json={"amount": 10},
            headers={"Idempotency-Key": str(uuid4())},
        )

    service_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "sync_service_called"
    )
    assert correlation_id_context.get() is None
    assert service_record is not None
    assert seen_correlation_ids == [response.headers["X-Request-ID"]]
    completion = _completion_records(caplog)[0]
    assert completion.correlation_id == response.headers["X-Request-ID"]


def test_business_rejection_logs_info_and_typed_metadata(
    client, caplog, monkeypatch
) -> None:
    outcome = _outcome(
        status=OperationStatus.REJECTED,
        error_code=BusinessErrorCode.ACCOUNT_NOT_FOUND,
    )
    monkeypatch.setattr(
        "server.app.routes.idempotency.coordinate_deposit",
        lambda *args: outcome,
    )

    with caplog.at_level(logging.INFO, logger="server.app.http"):
        response = client.post(
            "/accounts/7/deposit",
            json={"amount": 10},
            headers={"Idempotency-Key": str(uuid4())},
        )

    record = _completion_records(caplog)[0]
    assert response.status_code == 404
    assert record.levelno == logging.INFO
    assert record.business_status == "REJECTED"
    assert record.error_code == "ACCOUNT_NOT_FOUND"
    assert record.replayed is False


def test_validation_warning_omits_malformed_idempotency_value(client, caplog) -> None:
    malformed = "not-a-uuid-secret"
    with caplog.at_level(logging.INFO, logger="server.app.http"):
        response = client.post(
            "/accounts/7/deposit",
            json={"amount": 0},
            headers={"Idempotency-Key": malformed},
        )

    record = _completion_records(caplog)[0]
    assert response.status_code == 422
    assert record.levelno == logging.WARNING
    assert not hasattr(record, "idempotency_key")
    assert malformed not in caplog.text


def test_technical_failure_logs_safe_error_and_returns_correlation_header(
    client, caplog, monkeypatch
) -> None:
    def fail(*args):
        raise OperationalError("SECRET SQL", {}, OSError("password=secret"))

    monkeypatch.setattr("server.app.routes.idempotency.coordinate_deposit", fail)
    with caplog.at_level(logging.INFO):
        response = client.post(
            "/accounts/7/deposit",
            json={"amount": 10},
            headers={"Idempotency-Key": str(uuid4())},
        )

    completion = _completion_records(caplog)[0]
    failure = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "database_operation_failed"
    )
    assert response.status_code == 503
    assert UUID(response.headers["X-Request-ID"]).version == 4
    assert completion.levelno == logging.ERROR
    assert failure.exception_type == "OperationalError"
    assert not failure.exc_info
    assert "SECRET SQL" not in caplog.text
    assert "password" not in caplog.text


def test_unmatched_route_uses_safe_path_and_one_completion(client, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="server.app.http"):
        response = client.get("/user-controlled-secret-path")

    records = _completion_records(caplog)
    assert response.status_code == 404
    assert len(records) == 1
    assert records[0].path == "<unmatched>"
    assert "user-controlled" not in caplog.text


def test_compose_configures_rotation_only_for_api() -> None:
    compose = Path("infra/standalone/compose.yaml").read_text()

    api_section, database_section = compose.split("\n  db:\n", maxsplit=1)
    assert "driver: json-file" in api_section
    assert 'max-size: "10m"' in api_section
    assert 'max-file: "3"' in api_section
    assert "driver: json-file" not in database_section
