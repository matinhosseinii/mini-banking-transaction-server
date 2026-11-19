"""Gateway Uvicorn entry point."""

import os

import uvicorn

from server.app.logging_config import build_logging_config


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    uvicorn.run(
        "gateway.app:app",
        host="0.0.0.0",
        port=8000,
        access_log=False,
        log_config=build_logging_config(log_level),
    )


if __name__ == "__main__":
    main()
