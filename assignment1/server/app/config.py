"""Environment-backed application configuration."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Raised when an application configuration value is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuration needed by the application at runtime."""

    database_url: str
    max_concurrent_clients: int
    log_level: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build validated settings from an environment mapping."""
        values = os.environ if environ is None else environ
        database_url = values.get("DATABASE_URL", "").strip()
        if not database_url:
            msg = "DATABASE_URL must be set."
            raise ConfigurationError(msg)

        max_concurrent_clients = _positive_integer(
            values.get("MAX_CONCURRENT_CLIENTS", "100"), "MAX_CONCURRENT_CLIENTS"
        )
        log_level = _log_level(values.get("LOG_LEVEL", "INFO"))
        return cls(
            database_url=database_url,
            max_concurrent_clients=max_concurrent_clients,
            log_level=log_level,
        )


def _positive_integer(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        msg = f"{name} must be a positive integer."
        raise ConfigurationError(msg) from error

    if parsed <= 0:
        msg = f"{name} must be a positive integer."
        raise ConfigurationError(msg)
    return parsed


def _log_level(value: str) -> str:
    normalized = value.upper()
    if normalized not in logging.getLevelNamesMapping():
        msg = "LOG_LEVEL must be a supported logging level."
        raise ConfigurationError(msg)
    return normalized
