"""Unit tests for optional standalone demo seeding."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from server.app import seed
from server.app.banking import OperationStatus


def _outcome(account_id: int, *, replayed: bool):
    return SimpleNamespace(
        result=SimpleNamespace(
            status=OperationStatus.SUCCEEDED,
            account_id=account_id,
        ),
        replayed=replayed,
    )


def test_seed_definitions_and_request_ids_are_stable_and_distinct() -> None:
    assert [(item.name, item.initial_balance) for item in seed.DEMO_ACCOUNTS] == [
        ("Demo Account A", 10_000),
        ("Demo Account B", 5_000),
        ("Demo Account C", 0),
    ]
    request_ids = {
        seed.seed_request_id(operation, item.name)
        for item in seed.DEMO_ACCOUNTS
        for operation in ("create", "deposit")
    }
    assert len(request_ids) == 6
    assert all(
        isinstance(request_id, UUID) and request_id.version == 5
        for request_id in request_ids
    )
    assert seed.seed_request_id("create", "Demo Account A") == seed.seed_request_id(
        "create", "Demo Account A"
    )


def test_seed_uses_coordinator_and_skips_zero_balance_deposit(monkeypatch) -> None:
    creates = []
    deposits = []

    def create(session_factory, request_id, name):
        creates.append((session_factory, request_id, name))
        return _outcome(len(creates), replayed=False)

    def deposit(session_factory, request_id, account_id, amount):
        deposits.append((session_factory, request_id, account_id, amount))
        return _outcome(account_id, replayed=False)

    monkeypatch.setattr(seed, "coordinate_create_account", create)
    monkeypatch.setattr(seed, "coordinate_deposit", deposit)

    results = seed.seed_accounts("factory")

    assert [call[2] for call in creates] == [item.name for item in seed.DEMO_ACCOUNTS]
    assert [(call[2], call[3]) for call in deposits] == [(1, 10_000), (2, 5_000)]
    assert [result.deposit_replayed for result in results] == [False, False, None]


def test_repeated_or_partially_completed_seed_replays_same_operations(
    monkeypatch,
) -> None:
    calls = []

    def create(session_factory, request_id, name):
        calls.append(("create", request_id, name))
        account_id = [item.name for item in seed.DEMO_ACCOUNTS].index(name) + 10
        return _outcome(account_id, replayed=True)

    def deposit(session_factory, request_id, account_id, amount):
        calls.append(("deposit", request_id, account_id, amount))
        return _outcome(account_id, replayed=True)

    monkeypatch.setattr(seed, "coordinate_create_account", create)
    monkeypatch.setattr(seed, "coordinate_deposit", deposit)

    first = seed.seed_accounts("factory")
    first_calls = list(calls)
    calls.clear()
    second = seed.seed_accounts("factory")

    assert calls == first_calls
    assert first == second
    assert all(result.create_replayed for result in second)
    assert [result.deposit_replayed for result in second] == [True, True, None]


def test_seed_main_returns_nonzero_without_exposing_failure_details(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        seed.Settings,
        "from_environment",
        lambda: (_ for _ in ()).throw(RuntimeError("secret connection string")),
    )

    assert seed.main() == 1
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "secret connection string" not in captured.err


def test_image_and_scripts_use_explicit_temporary_compose_runs() -> None:
    dockerfile = Path("server/Dockerfile").read_text(encoding="utf-8")
    migration = Path("infra/standalone/scripts/migrate.sh").read_text(encoding="utf-8")
    seeding = Path("infra/standalone/scripts/seed.sh").read_text(encoding="utf-8")

    assert "server/alembic.ini" in dockerfile
    assert "server/migrations" in dockerfile
    assert "run --rm --no-deps api" in migration
    assert "alembic -c /app/server/alembic.ini upgrade head" in migration
    assert "run --rm --no-deps api" in seeding
    assert "python -m server.app.seed" in seeding
    assert "down -v" not in migration + seeding
