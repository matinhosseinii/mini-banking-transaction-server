"""Focused tests for the simple concurrent request generator."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import pytest

from client.config import Timeouts
from load_generator.cli import CliUsageError, format_human, parse_args
from load_generator.core import Consistency, Outcome, run_generator
from server.app.run import concurrency_limit


def _balance(value: int, request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"account_id": 7, "balance": value, "status": "ACTIVE"},
        request=request,
    )


def _success(request: httpx.Request, balance_after: int = 100) -> httpx.Response:
    operation = request.url.path.rsplit("/", 1)[-1].upper()
    return httpx.Response(
        200,
        json={
            "request_id": request.headers["Idempotency-Key"],
            "operation": operation,
            "requested_account_id": 7,
            "account_id": 7,
            "amount": 10,
            "status": "SUCCEEDED",
            "balance_after": balance_after,
        },
        request=request,
    )


def _run(scenario: str, handler, *, requests: int = 4, amount: int | None = 10):
    return asyncio.run(
        run_generator(
            base_url="http://service.test",
            service="standalone",
            scenario=scenario,
            account_id=7,
            requests=requests,
            concurrency=2,
            amount=amount,
            timeouts=Timeouts(),
            transport=httpx.MockTransport(handler),
        )
    )


@pytest.mark.parametrize("value", ["0", "-1", "text"])
@pytest.mark.parametrize("option", ["--account-id", "--requests", "--concurrency"])
def test_cli_rejects_nonpositive_numeric_inputs(option, value) -> None:
    argv = [
        "--scenario",
        "balance",
        "--account-id",
        "1",
        "--requests",
        "1",
        "--concurrency",
        "1",
    ]
    argv[argv.index(option) + 1] = value
    with pytest.raises(CliUsageError):
        parse_args(argv)


def test_cli_requires_amount_only_for_financial_scenarios() -> None:
    common = ["--account-id", "1", "--requests", "1", "--concurrency", "1"]
    assert parse_args(["--scenario", "balance", *common]).amount is None
    with pytest.raises(CliUsageError, match="--amount"):
        parse_args(["--scenario", "withdraw", *common])


def test_cli_preserves_selected_service_profile_and_config_path(tmp_path) -> None:
    args = parse_args(
        [
            "--scenario",
            "balance",
            "--account-id",
            "1",
            "--requests",
            "1",
            "--concurrency",
            "1",
            "--service",
            "ha",
            "--config",
            str(tmp_path / "client.json"),
        ]
    )

    assert args.service == "ha"
    assert args.config == tmp_path / "client.json"


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("balance", ["BALANCE"] * 4),
        ("deposit", ["DEPOSIT"] * 4),
        ("withdraw", ["WITHDRAW"] * 4),
        ("mixed", ["DEPOSIT", "WITHDRAW", "DEPOSIT", "WITHDRAW"]),
    ],
)
def test_all_scenarios_execute_exact_logical_operation_count(
    scenario, expected
) -> None:
    balance_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal balance_reads
        if request.method == "GET":
            balance_reads += 1
            return _balance(100, request)
        return _success(request)

    result = _run(scenario, handler, amount=None if scenario == "balance" else 10)

    assert [item.operation for item in result.operations] == expected
    assert result.total_requests == len(result.operations) == 4
    assert balance_reads == (4 if scenario == "balance" else 2)


def test_concurrency_is_bounded_and_financial_uuids_are_distinct_v4() -> None:
    active = 0
    maximum = 0
    ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        if request.method == "GET":
            return _balance(100, request)
        ids.append(request.headers["Idempotency-Key"])
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.005)
        active -= 1
        return _success(request)

    result = _run("deposit", handler, requests=12)

    assert maximum <= 2
    assert len(ids) == len(set(ids)) == 12
    assert all(UUID(value).version == 4 for value in ids)
    assert result.succeeded == 12


def test_transport_failures_are_unknown_and_never_retried() -> None:
    mutations = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mutations
        if request.method == "GET":
            return _balance(100, request)
        mutations += 1
        raise httpx.ConnectError("offline", request=request)

    result = _run("withdraw", handler, requests=3)

    assert mutations == 3
    assert result.unknown == 3
    assert result.consistency is Consistency.INCONCLUSIVE
    assert all(item.request_id for item in result.operations)


def test_business_rejection_is_confirmed_but_503_is_unknown() -> None:
    mutation_number = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mutation_number
        if request.method == "GET":
            return _balance(100, request)
        mutation_number += 1
        if mutation_number == 1:
            return httpx.Response(
                409,
                json={
                    "request_id": request.headers["Idempotency-Key"],
                    "error_code": "INSUFFICIENT_FUNDS",
                    "detail": "Insufficient funds.",
                },
                request=request,
            )
        return httpx.Response(503, json={"detail": "Overloaded."}, request=request)

    result = _run("withdraw", handler, requests=2)

    assert [item.outcome for item in result.operations] == [
        Outcome.REJECTED,
        Outcome.UNKNOWN,
    ]
    assert result.consistency is Consistency.INCONCLUSIVE


@pytest.mark.parametrize(
    ("final_balance", "unknown", "expected_consistency"),
    [
        (120, False, Consistency.PASS),
        (119, False, Consistency.FAIL),
        (120, True, Consistency.INCONCLUSIVE),
    ],
)
def test_expected_balance_and_consistency(
    final_balance, unknown, expected_consistency
) -> None:
    reads = iter([100, final_balance])
    mutation_number = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mutation_number
        if request.method == "GET":
            return _balance(next(reads), request)
        mutation_number += 1
        if unknown and mutation_number == 1:
            return httpx.Response(503, request=request)
        return _success(request)

    result = _run("deposit", handler, requests=2)

    assert result.expected_balance == (110 if unknown else 120)
    assert result.consistency is expected_consistency


def test_human_and_json_results_include_equivalent_counts() -> None:
    result = _run("balance", lambda request: _balance(100, request), amount=None)

    human = format_human(result)
    document = json.loads(json.dumps(result.to_dict()))

    assert "Scenario:         BALANCE" in human
    assert "Succeeded:        4" in human
    assert document["total_requests"] == 4
    assert document["succeeded"] == 4
    assert document["operations"][0]["outcome"] == "SUCCEEDED"


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, None),
        ({"UVICORN_LIMIT_CONCURRENCY": ""}, None),
        ({"UVICORN_LIMIT_CONCURRENCY": "100"}, 100),
    ],
)
def test_uvicorn_concurrency_limit(environment, expected) -> None:
    assert concurrency_limit(environment) == expected


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_uvicorn_concurrency_limit_must_be_positive(value) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        concurrency_limit({"UVICORN_LIMIT_CONCURRENCY": value})
