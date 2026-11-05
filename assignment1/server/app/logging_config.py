"""Safe JSON logging configuration for the application and Uvicorn."""

from __future__ import annotations

import json
import logging
import logging.config
import math
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

correlation_id_context: ContextVar[str | None] = ContextVar(
    "correlation_id", default=None
)

_STRUCTURED_FIELDS = (
    "event",
    "request_id",
    "correlation_id",
    "idempotency_key",
    "client_ip",
    "method",
    "path",
    "action",
    "account_id",
    "status_code",
    "result",
    "duration_ms",
    "operation",
    "business_status",
    "replayed",
    "error_code",
    "exception_type",
)


class JsonFormatter(logging.Formatter):
    """Format allowlisted operational metadata as one safe JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": _event_name(record),
        }
        correlation_id = getattr(record, "correlation_id", None)
        if correlation_id is None:
            correlation_id = correlation_id_context.get()
        if correlation_id is not None:
            document["correlation_id"] = _safe_value(correlation_id)

        for field in _STRUCTURED_FIELDS:
            if field in {"event", "correlation_id"}:
                continue
            value = getattr(record, field, None)
            if value is not None:
                document[field] = _safe_value(value)

        return json.dumps(document, separators=(",", ":"), ensure_ascii=False)


def configure_logging(log_level: str) -> None:
    """Configure application and Uvicorn loggers with one JSON schema."""
    logging.config.dictConfig(build_logging_config(log_level))


def build_logging_config(log_level: str) -> dict[str, Any]:
    """Build the dictConfig used by both local and Docker Uvicorn startup."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {"()": "server.app.logging_config.JsonFormatter"},
        },
        "handlers": {
            "json": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://sys.stdout",
            },
            "null": {"class": "logging.NullHandler"},
        },
        "root": {"handlers": ["json"], "level": log_level},
        "loggers": {
            "server": {
                "handlers": ["json"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn": {
                "handlers": ["json"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["json"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["null"],
                "level": log_level,
                "propagate": False,
            },
        },
    }


def _event_name(record: logging.LogRecord) -> str:
    explicit = getattr(record, "event", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    if record.name.startswith("uvicorn"):
        return _uvicorn_event(record.msg)
    return "application_log"


def _uvicorn_event(message_template: object) -> str:
    if not isinstance(message_template, str):
        return "uvicorn_log"
    known_events = {
        "Started server process [%d]": "uvicorn_server_process_started",
        "Waiting for application startup.": "uvicorn_application_starting",
        "Application startup complete.": "uvicorn_application_started",
        "Shutting down": "uvicorn_shutdown_started",
        "Waiting for application shutdown.": "uvicorn_application_stopping",
        "Application shutdown complete.": "uvicorn_application_stopped",
        "Finished server process [%d]": "uvicorn_server_process_stopped",
    }
    return known_events.get(message_template, "uvicorn_log")


def _safe_value(value: object) -> str | int | float | bool | None:
    if isinstance(value, float) and not math.isfinite(value):
        return "<float>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (UUID, Enum)):
        return str(value)
    return f"<{type(value).__name__}>"
