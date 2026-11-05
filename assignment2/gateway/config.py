"""Environment-backed Gateway configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class GatewayConfigurationError(ValueError):
    """Raised when Gateway configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ReplicaConfig:
    replica_id: str
    shard_id: str
    base_url: str


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    replicas: tuple[ReplicaConfig, ...]
    health_interval_seconds: float
    health_timeout_seconds: float

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> GatewaySettings:
        values = os.environ if environ is None else environ
        replicas = tuple(
            ReplicaConfig(replica_id, shard_id, _url(values, variable))
            for replica_id, shard_id, variable in (
                ("bank-a1", "A", "BANK_A1_URL"),
                ("bank-a2", "A", "BANK_A2_URL"),
                ("bank-b1", "B", "BANK_B1_URL"),
                ("bank-b2", "B", "BANK_B2_URL"),
            )
        )
        return cls(
            replicas=replicas,
            health_interval_seconds=_positive_float(
                values, "GATEWAY_HEALTH_INTERVAL_SECONDS", 5.0
            ),
            health_timeout_seconds=_positive_float(
                values, "GATEWAY_HEALTH_TIMEOUT_SECONDS", 1.0
            ),
        )


def _url(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise GatewayConfigurationError(f"{name} must be an HTTP URL.")
    return value


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(values.get(name, str(default)))
    except ValueError as error:
        raise GatewayConfigurationError(f"{name} must be positive.") from error
    if value <= 0:
        raise GatewayConfigurationError(f"{name} must be positive.")
    return value
