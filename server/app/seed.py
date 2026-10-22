"""Optional, replay-safe demo account seeding."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy.orm import Session, sessionmaker

from server.app.banking import OperationStatus
from server.app.config import Settings
from server.app.database import create_database_engine, create_session_factory
from server.app.idempotency import coordinate_create_account, coordinate_deposit

SEED_NAMESPACE = UUID("f4602cd7-faf7-4c98-81e7-62d1aeef19fd")


@dataclass(frozen=True, slots=True)
class DemoAccount:
    """Stable definition of one synthetic demonstration account."""

    name: str
    initial_balance: int


@dataclass(frozen=True, slots=True)
class SeedResult:
    """Replay information emitted after seeding one account."""

    name: str
    account_id: int
    create_replayed: bool
    deposit_replayed: bool | None


DEMO_ACCOUNTS = (
    DemoAccount("Demo Account A", 10_000),
    DemoAccount("Demo Account B", 5_000),
    DemoAccount("Demo Account C", 0),
)


def seed_request_id(operation: str, account_name: str) -> UUID:
    """Return a stable UUID for one versioned logical seed operation."""
    return uuid5(SEED_NAMESPACE, f"standalone-demo-v1:{operation}:{account_name}")


def seed_accounts(session_factory: sessionmaker[Session]) -> list[SeedResult]:
    """Create and fund all demo accounts through durable idempotency."""
    results = []
    for definition in DEMO_ACCOUNTS:
        created = coordinate_create_account(
            session_factory,
            seed_request_id("create", definition.name),
            definition.name,
        )
        if (
            created.result.status is not OperationStatus.SUCCEEDED
            or created.result.account_id is None
        ):
            raise RuntimeError(f"Unable to seed {definition.name}.")

        deposit_replayed = None
        if definition.initial_balance > 0:
            deposited = coordinate_deposit(
                session_factory,
                seed_request_id("deposit", definition.name),
                created.result.account_id,
                definition.initial_balance,
            )
            if deposited.result.status is not OperationStatus.SUCCEEDED:
                raise RuntimeError(f"Unable to fund {definition.name}.")
            deposit_replayed = deposited.replayed

        results.append(
            SeedResult(
                name=definition.name,
                account_id=created.result.account_id,
                create_replayed=created.replayed,
                deposit_replayed=deposit_replayed,
            )
        )
    return results


def _execution_label(replayed: bool | None) -> str:
    if replayed is None:
        return "not-required"
    return "replayed" if replayed else "new"


def main() -> int:
    """Seed using application configuration and return a shell-friendly status."""
    engine = None
    try:
        settings = Settings.from_environment()
        engine = create_database_engine(settings)
        results = seed_accounts(create_session_factory(engine))
    except Exception as error:  # noqa: BLE001 - utility boundary returns nonzero.
        print(f"Demo seed failed: {type(error).__name__}.", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print("Demo seed complete:")
    for result in results:
        print(
            f"- {result.name}: account_id={result.account_id} "
            f"create={_execution_label(result.create_replayed)} "
            f"deposit={_execution_label(result.deposit_replayed)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
