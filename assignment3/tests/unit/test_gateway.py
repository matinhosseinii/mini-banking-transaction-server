"""Focused Gateway routing, round-robin, and failover tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
from starlette.requests import Request

from gateway import app as gateway_app
from gateway.config import ReplicaConfig
from gateway.replicas import NoHealthyReplica, ReplicaPool

REPLICAS = (
    ReplicaConfig("a1", "A", "http://a1"),
    ReplicaConfig("a2", "A", "http://a2"),
    ReplicaConfig("b1", "B", "http://b1"),
    ReplicaConfig("b2", "B", "http://b2"),
)


def run(coroutine):
    return asyncio.run(coroutine)


def test_round_robin_is_independent_and_never_crosses_shards() -> None:
    async def scenario() -> None:
        pool = ReplicaPool(REPLICAS)
        for replica in REPLICAS:
            await pool.set_health(replica.replica_id, True)

        assert [(await pool.select("A")).replica_id for _ in range(4)] == [
            "a1",
            "a2",
            "a1",
            "a2",
        ]
        assert [(await pool.select("B")).replica_id for _ in range(4)] == [
            "b1",
            "b2",
            "b1",
            "b2",
        ]

    run(scenario())


def test_failed_replica_is_skipped_and_recovered_replica_rejoins() -> None:
    async def scenario() -> None:
        pool = ReplicaPool(REPLICAS)
        await pool.set_health("a1", True)
        await pool.set_health("a2", False)
        assert (await pool.select("A")).replica_id == "a1"
        assert (await pool.select("A")).replica_id == "a1"

        await pool.set_health("a2", True)
        assert (await pool.select("A")).replica_id == "a2"
        assert (await pool.select("A")).replica_id == "a1"

    run(scenario())


def test_no_healthy_replica_is_explicit() -> None:
    async def scenario() -> None:
        pool = ReplicaPool(REPLICAS)
        try:
            await pool.select("B")
        except NoHealthyReplica as error:
            assert error.args == ("B",)
        else:
            raise AssertionError("selection unexpectedly succeeded")

    run(scenario())


def test_forward_returns_503_when_shard_has_no_healthy_replica() -> None:
    async def scenario() -> None:
        pool = ReplicaPool(REPLICAS)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
        response = await gateway_app._forward(request, "B")
        assert response.status_code == 503
        assert b"No healthy replica is available for shard B" in response.body

    run(scenario())


def test_account_routes_resolve_both_shards(monkeypatch) -> None:
    selected = []

    async def forward(request, shard_id, **kwargs):
        selected.append(shard_id)
        return gateway_app.JSONResponse({"shard": shard_id})

    monkeypatch.setattr(gateway_app, "_forward", forward)
    request = SimpleNamespace()
    run(gateway_app.account_operation(request, 1))
    run(gateway_app.account_operation(request, 1001))
    assert selected == ["A", "B"]


def test_creation_policy_is_deterministic_and_uses_both_shards() -> None:
    identifiers = [UUID(int=value) for value in range(1, 100)]
    decisions = [gateway_app._creation_shard(value) for value in identifiers]

    assert set(decisions) == {"A", "B"}
    assert decisions == [gateway_app._creation_shard(value) for value in identifiers]


def test_forwarding_failure_is_not_retried_and_marks_replica_unhealthy() -> None:
    class FailingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def request(self, *args, **kwargs):
            self.calls += 1
            request = httpx.Request("POST", "http://a1/accounts/1/deposit")
            raise httpx.ConnectError("failed", request=request)

    async def scenario() -> None:
        pool = ReplicaPool(REPLICAS)
        await pool.set_health("a1", True)
        client = FailingClient()
        app = SimpleNamespace(state=SimpleNamespace(pool=pool, client=client))
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": b"{}", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/accounts/1/deposit",
                "query_string": b"",
                "headers": [],
                "app": app,
            },
            receive,
        )
        response = await gateway_app._send(request, REPLICAS[0])

        assert response.status_code == 502
        assert client.calls == 1
        assert (await pool.snapshot())["A"]["a1"] is False

    run(scenario())


def test_same_shard_transfer_routes_to_bank_and_cross_shard_to_coordinator(
    monkeypatch,
) -> None:
    selected = []
    coordinator_calls = []

    async def forward(request, shard_id, **kwargs):
        selected.append(shard_id)
        return gateway_app.JSONResponse({"shard": shard_id})

    class TransferRequest:
        def __init__(self, document):
            self.document = document
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(coordinator_url="http://coordinator")
                )
            )

        async def body(self):
            return json.dumps(self.document).encode()

    monkeypatch.setattr(gateway_app, "_forward", forward)

    async def send_to_url(request, base_url):
        coordinator_calls.append(base_url)
        return gateway_app.JSONResponse({"coordinator": True})

    monkeypatch.setattr(gateway_app, "_send_to_url", send_to_url)
    same_shard = run(
        gateway_app.transfer(
            TransferRequest(
                {
                    "source_account_id": 1,
                    "destination_account_id": 2,
                    "amount": 10,
                }
            )
        )
    )
    cross_shard = run(
        gateway_app.transfer(
            TransferRequest(
                {
                    "source_account_id": 1,
                    "destination_account_id": 1001,
                    "amount": 10,
                }
            )
        )
    )

    assert same_shard.status_code == 200
    assert selected == ["A"]
    assert cross_shard.status_code == 200
    assert coordinator_calls == ["http://coordinator"]


def test_positive_out_of_range_mutations_route_consistently(monkeypatch) -> None:
    selected = []

    async def forward(request, shard_id, **kwargs):
        selected.append(shard_id)
        return gateway_app.JSONResponse({"shard": shard_id})

    monkeypatch.setattr(gateway_app, "_forward", forward)
    request = SimpleNamespace()
    run(gateway_app.account_operation(request, 999999))
    run(gateway_app.account_operation(request, 999999))

    assert len(selected) == 2
    assert selected[0] == selected[1]
