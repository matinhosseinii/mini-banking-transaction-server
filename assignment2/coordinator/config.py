"""Environment configuration for the single Coordinator instance."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    shard_urls: dict[str, tuple[str, ...]]
    recovery_interval_seconds: float

    @classmethod
    def from_environment(cls) -> Settings:
        database_url = os.environ["DATABASE_URL"]
        return cls(
            database_url=database_url,
            shard_urls={
                "A": (_url("BANK_A1_URL"), _url("BANK_A2_URL")),
                "B": (_url("BANK_B1_URL"), _url("BANK_B2_URL")),
            },
            recovery_interval_seconds=float(
                os.environ.get("COORDINATOR_RECOVERY_INTERVAL_SECONDS", "5")
            ),
        )


def _url(name: str) -> str:
    value = os.environ[name].strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"{name} must be an HTTP URL.")
    return value
