"""Repeatable database initialization for one configured banking shard."""

from __future__ import annotations

import sys

from sqlalchemy import text

from server.app.config import Settings
from server.app.database import create_database_engine
from server.app.sharding import (
    SHARD_INITIALIZATION_LOCK,
    ShardDefinition,
    get_shard,
)

_RANGE_CONSTRAINT = "ck_accounts_local_shard"


def initialize_database(settings: Settings) -> None:
    """Install the local range constraint and align the identity sequence."""
    shard = get_shard(settings.shard_id)
    engine = create_database_engine(settings)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": SHARD_INITIALIZATION_LOCK},
            )
            outside_count = connection.execute(
                text(
                    "SELECT count(*) FROM accounts WHERE id < :minimum OR id > :maximum"
                ),
                {
                    "minimum": shard.minimum_account_id,
                    "maximum": shard.maximum_account_id,
                },
            ).scalar_one()
            if outside_count:
                raise RuntimeError("Database contains accounts owned by another shard.")

            constraint_exists = connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conname = :name AND conrelid = 'accounts'::regclass)"
                ),
                {"name": _RANGE_CONSTRAINT},
            ).scalar_one()
            if not constraint_exists:
                connection.exec_driver_sql(_constraint_sql(shard))

            sequence_name = connection.execute(
                text("SELECT pg_get_serial_sequence('accounts', 'id')")
            ).scalar_one()
            sequence_value = connection.execute(
                text("SELECT last_value FROM accounts_id_seq")
            ).scalar_one()
            if sequence_value < shard.minimum_account_id:
                connection.execute(
                    text("SELECT setval(CAST(:sequence AS regclass), :value, false)"),
                    {
                        "sequence": sequence_name,
                        "value": shard.minimum_account_id,
                    },
                )
    finally:
        engine.dispose()


def _constraint_sql(shard: ShardDefinition) -> str:
    return (
        f"ALTER TABLE accounts ADD CONSTRAINT {_RANGE_CONSTRAINT} "
        f"CHECK (id BETWEEN {shard.minimum_account_id} "
        f"AND {shard.maximum_account_id})"
    )


def main() -> int:
    """Initialize the configured shard and return a shell-friendly status."""
    try:
        settings = Settings.from_environment()
        initialize_database(settings)
    except Exception as error:  # noqa: BLE001 - utility boundary returns nonzero.
        print(f"Shard initialization failed: {type(error).__name__}.", file=sys.stderr)
        return 1
    print(f"Shard {settings.shard_id} database initialized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
