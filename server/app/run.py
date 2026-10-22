"""Uvicorn entry point with early structured logging configuration."""

import os

import uvicorn

from server.app.logging_config import build_logging_config


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
    )


if __name__ == "__main__":
    main()
