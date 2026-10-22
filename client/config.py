"""Configuration loading and validation for the banking client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("~/.mini-banking/config.json").expanduser()


class ConfigError(ValueError):
    """Raised when client configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Timeouts:
    connect: float = 3.0
    read: float = 15.0
    write: float = 5.0
    pool: float = 3.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    deadline_seconds: float = 90.0
    lookup_timeout_seconds: float = 10.0
    backoff_seconds: tuple[float, ...] = (5.0, 15.0)
    jitter_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class ClientConfig:
    default_service: str
    services: dict[str, str]
    pending_dir: Path
    timeouts: Timeouts
    retry: RetryPolicy

    def base_url(self, service: str | None) -> tuple[str, str]:
        selected = service or self.default_service
        try:
            return selected, self.services[selected]
        except KeyError:
            raise ConfigError(f"Unknown service profile: {selected}") from None


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ClientConfig:
    """Load a validated client configuration file."""
    try:
        document = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"Configuration file not found: {path.expanduser()}"
        ) from None
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(
            f"Unable to read valid JSON configuration: {path.expanduser()}"
        ) from error
    if not isinstance(document, dict):
        raise ConfigError("Configuration root must be an object.")

    default_service = _string(document, "default_service")
    raw_services = document.get("services")
    if not isinstance(raw_services, dict) or not raw_services:
        raise ConfigError("services must be a non-empty object.")
    services: dict[str, str] = {}
    for name, profile in raw_services.items():
        if not isinstance(name, str) or not isinstance(profile, dict):
            raise ConfigError("Each service profile must be an object.")
        base_url = _string(profile, "base_url").rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(f"Service {name} base_url must use HTTP or HTTPS.")
        services[name] = base_url
    if default_service not in services:
        raise ConfigError("default_service must identify a configured service.")

    raw_timeouts = _object(document, "timeouts", default={})
    timeouts = Timeouts(
        connect=_positive_number(raw_timeouts, "connect", 3),
        read=_positive_number(raw_timeouts, "read", 15),
        write=_positive_number(raw_timeouts, "write", 5),
        pool=_positive_number(raw_timeouts, "pool", 3),
    )
    raw_retry = _object(document, "retry", default={})
    max_attempts = _positive_integer(raw_retry, "max_attempts", 3)
    raw_backoff = raw_retry.get("backoff_seconds", [5, 15])
    if not isinstance(raw_backoff, list) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        for value in raw_backoff
    ):
        raise ConfigError(
            "retry.backoff_seconds must be a list of non-negative numbers."
        )
    if len(raw_backoff) < max_attempts - 1:
        raise ConfigError("retry.backoff_seconds must cover every retry attempt.")
    retry = RetryPolicy(
        max_attempts=max_attempts,
        deadline_seconds=_positive_number(raw_retry, "deadline_seconds", 90),
        lookup_timeout_seconds=_positive_number(
            raw_retry, "lookup_timeout_seconds", 10
        ),
        backoff_seconds=tuple(float(value) for value in raw_backoff),
        jitter_seconds=_nonnegative_number(raw_retry, "jitter_seconds", 1),
    )
    pending_value = document.get("pending_dir", "~/.mini-banking/pending")
    if not isinstance(pending_value, str) or not pending_value.strip():
        raise ConfigError("pending_dir must be a non-empty path string.")
    return ClientConfig(
        default_service=default_service,
        services=services,
        pending_dir=Path(pending_value).expanduser(),
        timeouts=timeouts,
        retry=retry,
    )


def _string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string.")
    return value


def _object(
    document: dict[str, Any], key: str, *, default: dict[str, Any]
) -> dict[str, Any]:
    value = document.get(key, default)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object.")
    return value


def _positive_number(document: dict[str, Any], key: str, default: float) -> float:
    value = document.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{key} must be a positive number.")
    return float(value)


def _nonnegative_number(document: dict[str, Any], key: str, default: float) -> float:
    value = document.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{key} must be a non-negative number.")
    return float(value)


def _positive_integer(document: dict[str, Any], key: str, default: int) -> int:
    value = document.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer.")
    return value
