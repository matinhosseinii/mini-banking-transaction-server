"""Allow durable same-shard transfer outcomes.

Revision ID: 0002_same_shard_transfers
Revises: 0001_initial_schema
Create Date: 2026-11-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_same_shard_transfers"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATION_ALLOWED = (
    "operation IN "
    "('CREATE_ACCOUNT', 'CLOSE_ACCOUNT', 'DEPOSIT', 'WITHDRAW', 'TRANSFER')"
)
_OPERATION_ARGUMENTS = (
    "((operation = 'CREATE_ACCOUNT' "
    "AND requested_account_id IS NULL AND amount IS NULL) OR "
    "(operation = 'CLOSE_ACCOUNT' "
    "AND requested_account_id IS NOT NULL AND amount IS NULL) OR "
    "(operation IN ('DEPOSIT', 'WITHDRAW', 'TRANSFER') "
    "AND requested_account_id IS NOT NULL AND amount > 0)) IS TRUE"
)
_RESULT_SHAPE = (
    "((status = 'SUCCEEDED' AND operation = 'CREATE_ACCOUNT' "
    "AND account_id IS NOT NULL AND requested_account_id IS NULL "
    "AND amount IS NULL AND balance_before IS NULL "
    "AND balance_after = 0 AND error_code IS NULL) OR "
    "(status = 'SUCCEEDED' AND operation = 'CLOSE_ACCOUNT' "
    "AND account_id IS NOT NULL AND balance_before = 0 "
    "AND balance_after = 0 AND error_code IS NULL) OR "
    "(status = 'SUCCEEDED' AND operation = 'DEPOSIT' "
    "AND account_id IS NOT NULL AND balance_before IS NOT NULL "
    "AND balance_after IS NOT NULL AND error_code IS NULL "
    "AND balance_after::numeric = balance_before::numeric + amount::numeric) OR "
    "(status = 'SUCCEEDED' AND operation IN ('WITHDRAW', 'TRANSFER') "
    "AND account_id IS NOT NULL AND balance_before IS NOT NULL "
    "AND balance_after IS NOT NULL AND error_code IS NULL "
    "AND balance_after::numeric = balance_before::numeric - amount::numeric) OR "
    "(status = 'REJECTED' "
    "AND operation IN ('CLOSE_ACCOUNT', 'DEPOSIT', 'WITHDRAW', 'TRANSFER') "
    "AND account_id IS NULL AND balance_before IS NULL "
    "AND balance_after IS NULL AND error_code = 'ACCOUNT_NOT_FOUND') OR "
    "(status = 'REJECTED' "
    "AND operation IN ('CLOSE_ACCOUNT', 'DEPOSIT', 'WITHDRAW') "
    "AND account_id IS NOT NULL AND balance_before = 0 "
    "AND balance_after = 0 AND error_code = 'ACCOUNT_ALREADY_CLOSED') OR "
    "(status = 'REJECTED' AND operation = 'TRANSFER' "
    "AND account_id IS NOT NULL AND balance_before IS NOT NULL "
    "AND balance_after IS NOT NULL AND balance_before = balance_after "
    "AND error_code = 'ACCOUNT_ALREADY_CLOSED') OR "
    "(status = 'REJECTED' AND operation IN ('WITHDRAW', 'TRANSFER') "
    "AND account_id IS NOT NULL AND balance_before IS NOT NULL "
    "AND balance_after IS NOT NULL AND balance_before = balance_after "
    "AND balance_before::numeric < amount::numeric "
    "AND error_code = 'INSUFFICIENT_FUNDS') OR "
    "(status = 'REJECTED' AND operation = 'CLOSE_ACCOUNT' "
    "AND account_id IS NOT NULL AND balance_before IS NOT NULL "
    "AND balance_after IS NOT NULL AND balance_before = balance_after "
    "AND balance_before > 0 AND error_code = 'NON_ZERO_BALANCE')) IS TRUE"
)


def upgrade() -> None:
    """Extend request constraints for transfer outcomes."""
    op.drop_constraint("ck_requests_operation_allowed", "requests", type_="check")
    op.drop_constraint("ck_requests_operation_arguments", "requests", type_="check")
    op.drop_constraint("ck_requests_result_shape", "requests", type_="check")
    op.create_check_constraint(
        "ck_requests_operation_allowed", "requests", _OPERATION_ALLOWED
    )
    op.create_check_constraint(
        "ck_requests_operation_arguments", "requests", _OPERATION_ARGUMENTS
    )
    op.create_check_constraint("ck_requests_result_shape", "requests", _RESULT_SHAPE)


def downgrade() -> None:
    """Restore Assignment 1 request constraints after removing transfers."""
    op.execute("DELETE FROM requests WHERE operation = 'TRANSFER'")
    op.drop_constraint("ck_requests_result_shape", "requests", type_="check")
    op.drop_constraint("ck_requests_operation_arguments", "requests", type_="check")
    op.drop_constraint("ck_requests_operation_allowed", "requests", type_="check")
    op.create_check_constraint(
        "ck_requests_operation_allowed",
        "requests",
        _OPERATION_ALLOWED.replace(", 'TRANSFER'", ""),
    )
    op.create_check_constraint(
        "ck_requests_operation_arguments",
        "requests",
        _OPERATION_ARGUMENTS.replace(", 'TRANSFER'", ""),
    )
    op.create_check_constraint("ck_requests_result_shape", "requests", _RESULT_SHAPE)
