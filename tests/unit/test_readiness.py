"""Unit tests for standalone application readiness."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Self
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from server.app import main
from server.app.config import ConfigurationError, Settings
from server.app.main import app


class FakeResult:
    def __init__(self, is_in_recovery: bool) -> None:
        self.is_in_recovery = is_in_recovery

    def scalar_one(self) -> bool:
        return self.is_in_recovery


class FakeSession:
    def __init__(self, is_in_recovery: bool) -> None:
        self.is_in_recovery = is_in_recovery

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def execute(self, statement: object) -> FakeResult:
        return FakeResult(self.is_in_recovery)


class FakeEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


def _configure_lifespan(monkeypatch, is_in_recovery: bool = False):
    engine = FakeEngine()
    session_factory = lambda: FakeSession(is_in_recovery)
    monkeypatch.setattr(
        main.Settings,
        "from_environment",
        lambda: Settings(
            database_url="postgresql+psycopg://user:password@localhost/database",
            max_concurrent_clients=100,
            log_level="INFO",
        ),
    )
    monkeypatch.setattr(main, "create_database_engine", lambda settings: engine)
    monkeypatch.setattr(main, "configure_logging", lambda log_level: None)
    monkeypatch.setattr(
        main, "create_session_factory", lambda created_engine: session_factory
    )
    return engine, session_factory


def test_application_imports_without_database_connectivity() -> None:
    assert app is not None


def test_ready_when_database_is_writable_primary(monkeypatch) -> None:
    _configure_lifespan(monkeypatch)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert UUID(response.headers["X-Request-ID"]).version == 4


def test_ready_is_unavailable_when_database_is_in_recovery(monkeypatch) -> None:
    # Recovery status is sufficient only for standalone mode, not Patroni HA.
    _configure_lifespan(monkeypatch, is_in_recovery=True)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "detail": "Database is not ready.",
    }
    assert UUID(response.headers["X-Request-ID"]).version == 4


def test_successful_ready_logs_info_completion(monkeypatch, caplog) -> None:
    _configure_lifespan(monkeypatch)

    with (
        caplog.at_level(logging.INFO, logger="server.app.http"),
        TestClient(app) as client,
    ):
        response = client.get("/ready")

    completion = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "http_request_completed"
    ]
    assert response.status_code == 200
    assert UUID(response.headers["X-Request-ID"]).version == 4
    assert len(completion) == 1
    assert completion[0].levelno == logging.INFO
    assert completion[0].request_id == response.headers["X-Request-ID"]
    assert completion[0].correlation_id == response.headers["X-Request-ID"]
    assert completion[0].client_ip == "testclient"
    assert completion[0].action == "READINESS"
    assert completion[0].status_code == 200
    assert completion[0].result == "SUCCEEDED"
    assert completion[0].duration_ms >= 0


def test_failed_ready_logs_error_completion(monkeypatch, caplog) -> None:
    _configure_lifespan(monkeypatch, is_in_recovery=True)

    with (
        caplog.at_level(logging.INFO, logger="server.app.http"),
        TestClient(app) as client,
    ):
        response = client.get("/ready")

    completion = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "http_request_completed"
    ]
    assert response.status_code == 503
    assert len(completion) == 1
    assert completion[0].levelno == logging.ERROR
    assert completion[0].request_id == response.headers["X-Request-ID"]
    assert completion[0].correlation_id == response.headers["X-Request-ID"]
    assert completion[0].client_ip == "testclient"
    assert completion[0].action == "READINESS"
    assert completion[0].status_code == 503
    assert completion[0].result == "INFRASTRUCTURE_ERROR"
    assert completion[0].duration_ms >= 0


def test_ready_is_unavailable_when_database_connection_fails(monkeypatch) -> None:
    _configure_lifespan(monkeypatch)

    @contextmanager
    def unavailable_session_scope(session_factory):
        raise OperationalError("SELECT 1", {}, OSError("database unavailable"))
        yield

    monkeypatch.setattr(main, "session_scope", unavailable_session_scope)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "detail": "Database is not ready.",
    }


def test_ready_is_unavailable_when_database_resources_are_missing(monkeypatch) -> None:
    _configure_lifespan(monkeypatch)

    with TestClient(app) as client:
        del app.state.session_factory
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "detail": "Database is not ready.",
    }


def test_lifespan_creates_one_engine_and_reuses_one_session_factory(
    monkeypatch,
) -> None:
    engine, session_factory = _configure_lifespan(monkeypatch)
    created_engines = []
    original_create_engine = main.create_database_engine

    def track_engine(settings):
        created_engines.append(settings)
        return original_create_engine(settings)

    monkeypatch.setattr(main, "create_database_engine", track_engine)

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/ready").status_code == 200
        assert app.state.session_factory is session_factory

    assert len(created_engines) == 1
    assert engine.dispose_calls == 1
    assert not hasattr(app.state, "session_factory")


def test_invalid_max_concurrent_clients_is_rejected() -> None:
    environment = {
        "DATABASE_URL": "postgresql+psycopg://user:password@localhost/database",
        "MAX_CONCURRENT_CLIENTS": "0",
        "LOG_LEVEL": "INFO",
    }

    try:
        Settings.from_environment(environment)
    except ConfigurationError as error:
        assert str(error) == "MAX_CONCURRENT_CLIENTS must be a positive integer."
    else:
        raise AssertionError("Invalid MAX_CONCURRENT_CLIENTS was accepted.")


def test_invalid_log_level_is_rejected() -> None:
    environment = {
        "DATABASE_URL": "postgresql+psycopg://user:password@localhost/database",
        "MAX_CONCURRENT_CLIENTS": "100",
        "LOG_LEVEL": "VERBOSE",
    }

    try:
        Settings.from_environment(environment)
    except ConfigurationError as error:
        assert str(error) == "LOG_LEVEL must be a supported logging level."
    else:
        raise AssertionError("Invalid LOG_LEVEL was accepted.")
