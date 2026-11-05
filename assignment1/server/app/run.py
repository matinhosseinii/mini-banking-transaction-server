"""Uvicorn entry point with early structured logging configuration."""

import os
from collections.abc import Mapping

import uvicorn

from server.app.logging_config import build_logging_config


def concurrency_limit(environ: Mapping[str, str] | None = None) -> int | None:
    """Read an optional positive Uvicorn concurrency limit."""
    values = os.environ if environ is None else environ
    raw_value = values.get("UVICORN_LIMIT_CONCURRENCY")
    if raw_value is None or not raw_value.strip():
        return None
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(
            "UVICORN_LIMIT_CONCURRENCY must be a positive integer."
        ) from error
    if value <= 0:
        raise ValueError("UVICORN_LIMIT_CONCURRENCY must be a positive integer.")
    return value


def main() -> None:
    """Run the Banking Server with JSON logs and no duplicate access log."""
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    uvicorn.run(
        "server.app.main:app",
        host="0.0.0.0",
        port=8000,
        workers=1,
        access_log=False,
        log_config=build_logging_config(log_level),
        limit_concurrency=concurrency_limit(),
    )


if __name__ == "__main__":
    main()
