"""Deterministic account-to-shard resolution shared by applications and Gateway."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_SHARD_MAP_PATH = Path(__file__).resolve().parents[1] / "shard_map.json"
SHARD_INITIALIZATION_LOCK = 7_331_002


class ShardConfigurationError(ValueError):
    """Raised when the shard map or configured shard identity is invalid."""


class InvalidAccountId(ValueError):
    """Raised when an account identifier is malformed or unsupported."""


class WrongShardError(ValueError):
    """Raised when an account is sent to a non-owning shard."""


@dataclass(frozen=True, slots=True)
class ShardDefinition:
    """One inclusive account range and its database service."""

    shard_id: str
    minimum_account_id: int
    maximum_account_id: int
    database_service: str

    def owns(self, account_id: int) -> bool:
        """Return whether this shard owns an internal integer account ID."""
        return self.minimum_account_id <= account_id <= self.maximum_account_id

    def require_owner(self, account_id: int) -> None:
        """Reject an account that belongs to another configured shard."""
        owner = resolve_shard(account_id)
        if owner.shard_id != self.shard_id:
            raise WrongShardError(
                f"Account {format_account_id(account_id)} belongs to shard "
                f"{owner.shard_id}, not shard {self.shard_id}."
            )


@dataclass(frozen=True, slots=True)
class ShardMap:
    """Validated account formatting rules and ordered shard definitions."""

    account_prefix: str
    account_digits: int
    shards: tuple[ShardDefinition, ...]


@lru_cache(maxsize=1)
def load_shard_map(path: Path = DEFAULT_SHARD_MAP_PATH) -> ShardMap:
    """Load and validate the simple JSON shard map."""
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        prefix = document["account_prefix"]
        digits = document["account_digits"]
        definitions = tuple(
            ShardDefinition(
                shard_id=shard_id,
                minimum_account_id=item["minimum_account_id"],
                maximum_account_id=item["maximum_account_id"],
                database_service=item["database_service"],
            )
            for shard_id, item in document["shards"].items()
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ShardConfigurationError("Shard map is invalid.") from error

    if (
        not isinstance(prefix, str)
        or len(prefix) != 1
        or not isinstance(digits, int)
        or digits <= 0
        or not definitions
    ):
        raise ShardConfigurationError("Shard map is invalid.")

    expected_minimum = 1
    for definition in definitions:
        if (
            not definition.shard_id
            or definition.minimum_account_id != expected_minimum
            or definition.maximum_account_id < definition.minimum_account_id
            or not definition.database_service
        ):
            raise ShardConfigurationError("Shard ranges must be contiguous.")
        expected_minimum = definition.maximum_account_id + 1
    return ShardMap(prefix, digits, definitions)


def parse_account_id(account_id: str | int) -> int:
    """Convert a canonical public or internal account ID to an integer."""
    shard_map = load_shard_map()
    if isinstance(account_id, bool):
        raise InvalidAccountId("Account ID is invalid.")
    if isinstance(account_id, int):
        parsed = account_id
    elif (
        isinstance(account_id, str)
        and len(account_id) == 1 + shard_map.account_digits
        and account_id.startswith(shard_map.account_prefix)
        and account_id[1:].isdigit()
    ):
        parsed = int(account_id[1:])
    else:
        raise InvalidAccountId("Account ID must use the form A0001 through A2000.")

    if not any(definition.owns(parsed) for definition in shard_map.shards):
        raise InvalidAccountId("Account ID must be in the range A0001 through A2000.")
    return parsed


def format_account_id(account_id: int) -> str:
    """Format a validated internal ID as its canonical public identifier."""
    parsed = parse_account_id(account_id)
    shard_map = load_shard_map()
    return f"{shard_map.account_prefix}{parsed:0{shard_map.account_digits}d}"


def resolve_shard(account_id: str | int) -> ShardDefinition:
    """Return the sole authoritative shard for an account identifier."""
    parsed = parse_account_id(account_id)
    for definition in load_shard_map().shards:
        if definition.owns(parsed):
            return definition
    raise InvalidAccountId("Account ID is outside the supported range.")


def get_shard(shard_id: str) -> ShardDefinition:
    """Return a configured shard by identity."""
    normalized = shard_id.strip().upper()
    for definition in load_shard_map().shards:
        if definition.shard_id == normalized:
            return definition
    raise ShardConfigurationError(f"Unknown shard identity: {shard_id!r}.")
