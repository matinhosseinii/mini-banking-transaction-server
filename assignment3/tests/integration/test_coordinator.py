"""Focused PostgreSQL tests for durable cross-shard coordination."""

from __future__ import annotations

import json
import os
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import NoReturn
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from coordinator.models import Base, CoordinatedTransfer
from coordinator.service import Coordinator, CoordinatorConflict


@contextmanager
def _temporary_database(admin_url: str) -> Generator[str, None, None]:
    database_name = f"coordinator_test_{uuid4().hex}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    url = make_url(admin_url).set(database=database_name)
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        yield url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin.dispose()


@pytest.fixture(scope="module")
def coordinator_engine() -> Iterator[Engine]:
    admin_url = os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if admin_url is None:
        pytest.skip("TEST_POSTGRES_ADMIN_URL is required for Coordinator tests")
    with _temporary_database(admin_url) as database_url:
        engine = create_engine(database_url)
        Base.metadata.create_all(engine)
        try:
            yield engine
        finally:
            engine.dispose()


class Participants:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], dict] = {}
        self.prepare_calls = 0
        self.insufficient = False
        self.block_destination_commit = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        shard = "A" if request.url.host.startswith("a") else "B"
        transaction_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "POST":
            body = json.loads(request.content)
            transaction_id = body["transaction_id"]
            action = request.url.path.rsplit("/", 1)[-1]
            key = (shard, transaction_id)
            if action == "prepare":
                self.prepare_calls += 1
                if shard == "A" and self.insufficient:
                    result = {
                        "state": "REJECTED",
                        "error_code": "INSUFFICIENT_FUNDS",
                        "balance_before": 5,
                        "balance_after": 5,
                    }
                else:
                    result = {
                        "state": "PREPARED",
                        "error_code": None,
                        "balance_before": 100 if shard == "A" else 0,
                        "balance_after": 90 if shard == "A" else 10,
                    }
                self.states[key] = result
                return httpx.Response(200, json=result)
            if action == "commit" and shard == "B" and self.block_destination_commit:
                raise httpx.ConnectError("unavailable", request=request)
            current = self.states.get(key, {"state": "UNKNOWN"})
            if action == "commit" and current["state"] in {"PREPARED", "COMMITTED"}:
                current = {**current, "state": "COMMITTED"}
            elif action == "abort":
                current = {**current, "state": "ABORTED"}
            self.states[key] = current
            return httpx.Response(200, json=current)
        return httpx.Response(
            200,
            json=self.states.get((shard, transaction_id), {"state": "UNKNOWN"}),
        )


def _coordinator(
    engine: Engine,
    participants: Participants,
    after_commit_hook=None,
) -> Coordinator:
    client = httpx.Client(transport=httpx.MockTransport(participants))
    return Coordinator(
        engine,
        {"A": ("http://a1", "http://a2"), "B": ("http://b1", "http://b2")},
        client,
        after_commit_hook=after_commit_hook,
    )


def test_cross_shard_commit_replay_and_payload_conflict(coordinator_engine) -> None:
    participants = Participants()
    coordinator = _coordinator(coordinator_engine, participants)
    transaction_id = uuid4()

    first = coordinator.transfer(transaction_id, 1, 1001, 10)
    restarted = _coordinator(coordinator_engine, participants)
    replay = restarted.transfer(transaction_id, 1, 1001, 10)

    assert first.decision == "COMMIT"
    assert first.state == "RESOLVED"
    assert replay.replayed is True
    assert participants.prepare_calls == 2
    assert first.balance_before - first.balance_after == 10
    source = participants.states[("A", str(transaction_id))]
    destination = participants.states[("B", str(transaction_id))]
    assert (source["balance_after"] - source["balance_before"]) + (
        destination["balance_after"] - destination["balance_before"]
    ) == 0
    with pytest.raises(CoordinatorConflict):
        coordinator.transfer(transaction_id, 1, 1001, 11)


def test_concurrent_duplicate_uses_one_prepare_pair(coordinator_engine) -> None:
    participants = Participants()
    coordinator = _coordinator(coordinator_engine, participants)
    transaction_id = uuid4()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda _: coordinator.transfer(transaction_id, 4, 1004, 10),
                range(2),
            )
        )

    assert {outcome.decision for outcome in outcomes} == {"COMMIT"}
    assert participants.prepare_calls == 2


def test_concurrent_overlapping_cross_shard_transactions_resolve(
    coordinator_engine,
) -> None:
    participants = Participants()
    coordinator = _coordinator(coordinator_engine, participants)
    transaction_ids = [uuid4() for _ in range(10)]

    with ThreadPoolExecutor(max_workers=5) as executor:
        outcomes = list(
            executor.map(
                lambda transaction_id: coordinator.transfer(transaction_id, 6, 1006, 1),
                transaction_ids,
            )
        )

    assert all(outcome.state == "RESOLVED" for outcome in outcomes)
    assert all(outcome.decision == "COMMIT" for outcome in outcomes)
    assert participants.prepare_calls == 20
    assert all(
        participants.states[(shard, str(transaction_id))]["state"] == "COMMITTED"
        for transaction_id in transaction_ids
        for shard in ("A", "B")
    )


def test_rejected_prepare_durably_aborts_without_credit(coordinator_engine) -> None:
    participants = Participants()
    participants.insufficient = True
    coordinator = _coordinator(coordinator_engine, participants)

    outcome = coordinator.transfer(uuid4(), 2, 1002, 10)

    assert outcome.decision == "ABORT"
    assert outcome.state == "RESOLVED"
    assert outcome.error_code == "INSUFFICIENT_FUNDS"
    assert participants.prepare_calls == 1


def test_durable_commit_recovers_when_participant_returns(coordinator_engine) -> None:
    participants = Participants()
    participants.block_destination_commit = True
    coordinator = _coordinator(coordinator_engine, participants)
    transaction_id = uuid4()

    interrupted = coordinator.transfer(transaction_id, 3, 1003, 10)

    assert interrupted.decision == "COMMIT"
    assert interrupted.state == "DECIDED"
    participants.block_destination_commit = False
    recovered = coordinator.lookup(transaction_id)
    assert recovered is not None
    assert recovered.state == "RESOLVED"
    assert recovered.decision == "COMMIT"


def test_crash_after_durable_commit_recovers_same_transaction(
    coordinator_engine,
) -> None:
    class InjectedCrash(RuntimeError):
        pass

    participants = Participants()
    transaction_id = uuid4()

    def interrupt(committed_id) -> NoReturn:
        assert committed_id == transaction_id
        raise InjectedCrash

    interrupted = _coordinator(coordinator_engine, participants, interrupt)
    with pytest.raises(InjectedCrash):
        interrupted.transfer(transaction_id, 5, 1005, 10)

    with Session(coordinator_engine) as session:
        durable = session.get(CoordinatedTransfer, transaction_id)
    assert durable is not None
    assert durable.decision == "COMMIT"
    assert durable.state == "DECIDED"
    assert participants.states[("A", str(transaction_id))]["state"] == "PREPARED"
    assert participants.states[("B", str(transaction_id))]["state"] == "PREPARED"

    restarted = _coordinator(coordinator_engine, participants)
    restarted.recover()
    recovered = restarted.lookup(transaction_id)
    assert recovered is not None
    assert recovered.transaction_id == transaction_id
    assert recovered.decision == "COMMIT"
    assert recovered.state == "RESOLVED"
    assert participants.states[("A", str(transaction_id))]["state"] == "COMMITTED"
    assert participants.states[("B", str(transaction_id))]["state"] == "COMMITTED"
