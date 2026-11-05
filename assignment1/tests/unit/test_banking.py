"""Unit tests for Banking Service validation and result behavior."""

from unittest.mock import create_autospec

import pytest
from sqlalchemy.orm import Session

from server.app.banking import (
    BankingValidationError,
    BusinessErrorCode,
    OperationStatus,
    create_account,
    deposit,
    withdraw,
)


@pytest.mark.parametrize("name", ["", "   ", "\t\n", None])
def test_create_account_rejects_blank_or_non_string_names(name) -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(BankingValidationError):
        create_account(session, name)

    session.add.assert_not_called()
    session.flush.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "1", None])
@pytest.mark.parametrize("operation", [deposit, withdraw])
def test_financial_operations_reject_invalid_amounts(operation, amount) -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(BankingValidationError):
        operation(session, 1, amount)

    session.scalar.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_business_rejection_is_returned_without_transaction_control() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.return_value = None

    result = deposit(session, 404, 10)

    assert result.status is OperationStatus.REJECTED
    assert result.account_id is None
    assert result.balance_before is None
    assert result.balance_after is None
    assert result.error_code is BusinessErrorCode.ACCOUNT_NOT_FOUND
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
