"""Focused tests for deterministic account ownership."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.app.dependencies import get_local_shard
from server.app.sharding import (
    InvalidAccountId,
    WrongShardError,
    format_account_id,
    get_shard,
    parse_account_id,
    resolve_shard,
)


@pytest.mark.parametrize(
    ("account_id", "internal_id", "shard_id"),
    [
        ("A0001", 1, "A"),
        ("A1000", 1000, "A"),
        ("A1001", 1001, "B"),
        ("A2000", 2000, "B"),
    ],
)
def test_boundary_accounts_have_exactly_one_owner(
    account_id, internal_id, shard_id
) -> None:
    assert parse_account_id(account_id) == internal_id
    assert format_account_id(internal_id) == account_id
    assert resolve_shard(account_id).shard_id == shard_id
    assert resolve_shard(internal_id).shard_id == shard_id


@pytest.mark.parametrize(
    "account_id",
    ["A0000", "A2001", "B0001", "A001", "A00001", "a0001", "", 0, 2001, True],
)
def test_invalid_and_out_of_range_accounts_are_rejected(account_id) -> None:
    with pytest.raises(InvalidAccountId):
        resolve_shard(account_id)


def test_wrong_shard_is_rejected() -> None:
    with pytest.raises(WrongShardError):
        get_shard("A").require_owner(1001)
    with pytest.raises(WrongShardError):
        get_shard("B").require_owner(1000)


def test_local_shard_accepts_its_complete_range_boundaries() -> None:
    get_shard("A").require_owner(1)
    get_shard("A").require_owner(1000)
    get_shard("B").require_owner(1001)
    get_shard("B").require_owner(2000)


def test_missing_application_shard_state_fails_explicitly() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(HTTPException) as captured:
        get_local_shard(request)

    assert captured.value.status_code == 503
    assert captured.value.detail == "Shard configuration is not ready."
