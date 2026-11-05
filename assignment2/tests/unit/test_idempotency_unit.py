"""Unit checks for deterministic idempotency primitives and validation."""

import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from server.app.banking import BankingValidationError
from server.app.idempotency import (
    Operation,
    _advisory_lock_key,
    _canonical_payload_hash,
    coordinate_create_account,
    coordinate_deposit,
)
from server.app.models import Request


def test_advisory_lock_key_is_stable_signed_64_bit_value() -> None:
    request_id = UUID("12345678-1234-5678-1234-567812345678")

    first = _advisory_lock_key(request_id)
    second = _advisory_lock_key(request_id)

    assert first == second
    assert first == 4378530433401468378
    assert -(2**63) <= first < 2**63
    assert first != _advisory_lock_key(uuid4())


def test_payload_hash_uses_compact_sorted_utf8_json() -> None:
    payload = {
        "name": "Café Account",
        "operation": Operation.CREATE_ACCOUNT,
    }
    canonical = '{"name":"Café Account","operation":"CREATE_ACCOUNT"}'

    assert (
        _canonical_payload_hash(payload)
        == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )


def test_payload_hash_preserves_meaningful_name_differences() -> None:
    composed = _canonical_payload_hash(
        {"operation": Operation.CREATE_ACCOUNT, "name": "Café Account"}
    )
    decomposed = _canonical_payload_hash(
        {"operation": Operation.CREATE_ACCOUNT, "name": "Cafe\u0301 Account"}
    )
    extra_space = _canonical_payload_hash(
        {"operation": Operation.CREATE_ACCOUNT, "name": "Café  Account"}
    )

    assert len({composed, decomposed, extra_space}) == 3


def test_identical_duplicate_locks_then_replays_without_banking_call() -> None:
    request_id = uuid4()
    payload_hash = _canonical_payload_hash(
        {
            "operation": Operation.DEPOSIT,
            "requested_account_id": 12,
            "amount": 5,
        }
    )
    existing = Request(
        request_id=request_id,
        requested_account_id=12,
        account_id=12,
        operation=Operation.DEPOSIT,
        amount=5,
        payload_hash=payload_hash,
        status="SUCCEEDED",
        balance_before=10,
        balance_after=15,
        error_code=None,
        created_at=datetime.now(UTC),
    )
    session = MagicMock()
    session.scalar.return_value = existing
    transaction = MagicMock()
    transaction.__enter__.return_value = session
    session_factory = MagicMock()
    session_factory.begin.return_value = transaction

    with patch("server.app.idempotency.banking.deposit") as banking_deposit:
        outcome = coordinate_deposit(session_factory, request_id, 12, 5)

    assert outcome.replayed is True
    assert outcome.result.balance_after == 15
    assert session.method_calls[0][0] == "execute"
    assert session.method_calls[1][0] == "scalar"
    banking_deposit.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.parametrize("request_id", [None, "not-a-uuid", 1])
def test_invalid_request_id_is_rejected_before_opening_transaction(request_id) -> None:
    session_factory = MagicMock()

    with pytest.raises(BankingValidationError):
        coordinate_create_account(session_factory, request_id, "Valid Name")

    session_factory.begin.assert_not_called()


@pytest.mark.parametrize("name", ["", "  ", "\t\n", "x" * 101, None])
def test_invalid_name_is_rejected_before_opening_transaction(name) -> None:
    session_factory = MagicMock()

    with pytest.raises(BankingValidationError):
        coordinate_create_account(session_factory, uuid4(), name)

    session_factory.begin.assert_not_called()


@pytest.mark.parametrize("account_id", [0, -1, True, "1", None])
@pytest.mark.parametrize("amount", [1])
def test_invalid_account_id_is_rejected_before_transaction(account_id, amount) -> None:
    session_factory = MagicMock()

    with pytest.raises(BankingValidationError):
        coordinate_deposit(session_factory, uuid4(), account_id, amount)

    session_factory.begin.assert_not_called()


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "1", None])
def test_invalid_amount_is_rejected_before_opening_transaction(amount) -> None:
    session_factory = MagicMock()

    with pytest.raises(BankingValidationError):
        coordinate_deposit(session_factory, uuid4(), 1, amount)

    session_factory.begin.assert_not_called()
