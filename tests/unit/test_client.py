"""Unit tests for the command-only Banking Client."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from client import cli
from client.config import ClientConfig, ConfigError, RetryPolicy, Timeouts, load_config
from client.http import BankingHttpClient, ResultKind
from client.pending import PendingRequest, PendingStore, PendingStoreError


def _config(tmp_path: Path) -> ClientConfig:
    return ClientConfig(
        default_service="standalone",
        services={
            "standalone": "http://standalone.test",
            "ha": "http://ha.test",
        },
        pending_dir=tmp_path / "pending",
        timeouts=Timeouts(),
        retry=RetryPolicy(
            max_attempts=3,
            deadline_seconds=90,
            lookup_timeout_seconds=10,
            backoff_seconds=(5, 15),
            jitter_seconds=0,
        ),
    )


def _pending(request_id: UUID | None = None) -> PendingRequest:
    return PendingRequest.create(
        request_id=request_id or uuid4(),
        service="standalone",
        operation="DEPOSIT",
        method="POST",
        path="/accounts/7/deposit",
        payload={"amount": 10},
    )


def _response(status: int, document: dict) -> httpx.Response:
    return httpx.Response(status, json=document)


def _outcome(
    pending: PendingRequest,
    *,
    status: str = "SUCCEEDED",
    replayed: bool | None = None,
    request_id: UUID | None = None,
    operation: str | None = None,
) -> dict:
    document = {
        "request_id": str(request_id or pending.request_id),
        "operation": operation or pending.operation,
        "requested_account_id": 7,
        "account_id": 7,
        "amount": 10,
        "status": status,
        "balance_before": 0,
        "balance_after": 10 if status == "SUCCEEDED" else 0,
        "error_code": None if status == "SUCCEEDED" else "INSUFFICIENT_FUNDS",
        "created_at": datetime.now(UTC).isoformat(),
    }
    if replayed is not None:
        document["replayed"] = replayed
    return document


def test_configuration_loads_profiles_timeouts_and_retry_settings(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "default_service": "standalone",
                "services": {
                    "standalone": {"base_url": "http://server-a:8000/"},
                    "ha": {"base_url": "http://witness:9000"},
                },
                "pending_dir": str(tmp_path / "requests"),
                "timeouts": {"connect": 1, "read": 2, "write": 3, "pool": 4},
                "retry": {
                    "max_attempts": 2,
                    "deadline_seconds": 20,
                    "lookup_timeout_seconds": 6,
                    "backoff_seconds": [1],
                    "jitter_seconds": 0,
                },
            }
        )
    )

    config = load_config(path)

    assert config.base_url(None) == ("standalone", "http://server-a:8000")
    assert config.base_url("ha") == ("ha", "http://witness:9000")
    assert config.timeouts == Timeouts(connect=1, read=2, write=3, pool=4)
    assert config.retry.max_attempts == 2


def test_httpx_uses_each_configured_timeout() -> None:
    client = BankingHttpClient(
        "http://service.test",
        Timeouts(connect=1, read=2, write=3, pool=4),
        RetryPolicy(),
        transport=httpx.MockTransport(lambda request: _response(200, {})),
    )

    assert client.client.timeout.connect == 1
    assert client.client.timeout.read == 2
    assert client.client.timeout.write == 3
    assert client.client.timeout.pool == 4
    client.close()


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"default_service": "missing", "services": {}},
        {
            "default_service": "standalone",
            "services": {"standalone": {"base_url": "ftp://invalid"}},
        },
    ],
)
def test_invalid_configuration_is_rejected(tmp_path, document) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ConfigError):
        load_config(path)


def test_pending_store_writes_atomically_with_restrictive_permissions(tmp_path) -> None:
    store = PendingStore(tmp_path / "pending")
    pending = _pending()

    with store.locked():
        store.save(pending)
        loaded = store.get(pending.request_id)

    path = tmp_path / "pending" / "standalone" / f"{pending.request_id}.json"
    assert loaded == pending
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(path.parent.glob("*.tmp"))


def test_pending_store_never_overwrites_existing_uuid(tmp_path) -> None:
    store = PendingStore(tmp_path / "pending")
    original = _pending()
    conflicting = PendingRequest.create(
        request_id=original.request_id,
        service=original.service,
        operation="WITHDRAW",
        method="POST",
        path="/accounts/7/withdraw",
        payload={"amount": 99},
    )

    with store.locked():
        store.save(original)
        original_bytes = (
            tmp_path / "pending" / "standalone" / f"{original.request_id}.json"
        ).read_bytes()
        with pytest.raises(PendingStoreError, match="pending check"):
            store.save(conflicting)

    assert store.get(original.request_id) == original
    assert (
        tmp_path / "pending" / "standalone" / f"{original.request_id}.json"
    ).read_bytes() == original_bytes


def test_pending_store_prevents_a_second_process_owner(tmp_path) -> None:
    first = PendingStore(tmp_path / "pending")
    second = PendingStore(tmp_path / "pending")

    with (
        first.locked(),
        pytest.raises(PendingStoreError, match="locked"),
        second.locked(),
    ):
        pytest.fail("second lock unexpectedly succeeded")


def test_transient_retries_reuse_uuid_and_payload_then_succeed() -> None:
    pending = _pending()
    requests: list[httpx.Request] = []
    statuses = iter([503, 502, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = next(statuses)
        return _response(status, _outcome(pending, replayed=False))

    delays = []
    client = BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(jitter_seconds=0),
        transport=httpx.MockTransport(handler),
        sleep=delays.append,
        jitter=lambda low, high: 0,
    )

    result = client.mutate(pending, lambda message: None)
    client.close()

    assert result.kind is ResultKind.SUCCESS
    assert delays == [5, 15]
    assert len(requests) == 3
    assert {request.headers["Idempotency-Key"] for request in requests} == {
        str(pending.request_id)
    }
    assert {request.content for request in requests} == {b'{"amount":10}'}


def test_exhausted_failures_perform_one_lookup_and_return_unknown() -> None:
    pending = _pending()
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "GET":
            return _response(404, {"detail": "Request not found."})
        raise httpx.ConnectError("offline", request=request)

    client = BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(jitter_seconds=0),
        transport=httpx.MockTransport(handler),
        sleep=lambda delay: None,
        jitter=lambda low, high: 0,
    )

    result = client.mutate(pending, lambda message: None)
    client.close()

    assert result.kind is ResultKind.UNKNOWN
    assert paths == [
        pending.path,
        pending.path,
        pending.path,
        f"/requests/{pending.request_id}",
    ]


def test_overall_deadline_prevents_another_mutation_attempt() -> None:
    pending = _pending()
    methods = []
    clock_values = iter([0.0, 0.0, 91.0])

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return _response(404, {})
        raise httpx.ConnectError("offline", request=request)

    with BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(jitter_seconds=0),
        transport=httpx.MockTransport(handler),
        clock=lambda: next(clock_values),
        sleep=lambda delay: pytest.fail("deadline must prevent sleep"),
        jitter=lambda low, high: 0,
    ) as client:
        result = client.mutate(pending, lambda message: None)

    assert result.kind is ResultKind.UNKNOWN
    assert methods == ["POST", "GET"]


def test_http_500_uses_lookup_without_resending_mutation() -> None:
    pending = _pending()
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return _response(500, {})
        return _response(200, _outcome(pending))

    with BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.mutate(pending, lambda message: None)

    assert result.kind is ResultKind.SUCCESS
    assert methods == ["POST", "GET"]


@pytest.mark.parametrize(
    ("lookup_status", "expected"),
    [
        ("SUCCEEDED", ResultKind.SUCCESS),
        ("REJECTED", ResultKind.REJECTED),
    ],
)
def test_http_501_looks_up_without_retry_and_resolves(lookup_status, expected) -> None:
    pending = _pending()
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return _response(501, {})
        return _response(200, _outcome(pending, status=lookup_status))

    with BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.mutate(pending, lambda message: None)

    assert result.kind is expected
    assert methods == ["POST", "GET"]


def test_http_501_with_missing_lookup_remains_unknown() -> None:
    pending = _pending()
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return _response(501, {}) if request.method == "POST" else _response(404, {})

    with BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.mutate(pending, lambda message: None)

    assert result.kind is ResultKind.UNKNOWN
    assert methods == ["POST", "GET"]


def test_http_307_is_not_followed_and_valid_lookup_resolves() -> None:
    pending = _pending()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(307, headers={"Location": "/redirect-target"})
        return _response(200, _outcome(pending))

    with BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.mutate(pending, lambda message: None)

    assert result.kind is ResultKind.SUCCESS
    assert requests == [
        ("POST", pending.path),
        ("GET", f"/requests/{pending.request_id}"),
    ]


def test_http_307_unknown_lookup_keeps_cli_pending_record(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    request_id = uuid4()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(307, headers={"Location": "/redirect-target"})
        return _response(404, {"detail": "Request not found."})

    def client_factory(base_url, timeouts, retry):
        return BankingHttpClient(
            base_url,
            timeouts,
            retry,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "BankingHttpClient", client_factory)

    code = cli.main(
        [
            "deposit",
            "--account-id",
            "7",
            "--amount",
            "10",
            "--request-id",
            str(request_id),
        ]
    )

    assert code == cli.ExitCode.UNKNOWN
    pending = PendingStore(config.pending_dir).get(request_id)
    assert pending is not None
    assert pending.payload == {"amount": 10}
    assert requests == [
        ("POST", "/accounts/7/deposit"),
        ("GET", f"/requests/{request_id}"),
    ]


@pytest.mark.parametrize(
    "mutation_response",
    [
        lambda pending: _response(200, {}),
        lambda pending: httpx.Response(200, content=b"not-json"),
        lambda pending: _response(
            200, _outcome(pending, request_id=uuid4(), replayed=False)
        ),
        lambda pending: _response(
            200, _outcome(pending, operation="WITHDRAW", replayed=False)
        ),
    ],
)
def test_invalid_success_response_uses_lookup(mutation_response) -> None:
    pending = _pending()
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return mutation_response(pending)
        return _response(200, _outcome(pending))

    with BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.mutate(pending, lambda message: None)

    assert result.kind is ResultKind.SUCCESS
    assert methods == ["POST", "GET"]


def test_malformed_lookup_response_leaves_outcome_unknown() -> None:
    pending = _pending()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _response(200, {})
        return _response(200, {"status": "SUCCEEDED"})

    with BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.mutate(pending, lambda message: None)

    assert result.kind is ResultKind.UNKNOWN


def test_business_rejection_and_validation_are_not_retried() -> None:
    for status, document, expected in (
        (
            409,
            {"error_code": "INSUFFICIENT_FUNDS", "detail": "No funds."},
            ResultKind.REJECTED,
        ),
        (422, {"detail": "Invalid."}, ResultKind.CLIENT_ERROR),
    ):
        calls = 0

        def handler(
            request: httpx.Request, status=status, document=document
        ) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _response(status, document)

        with BankingHttpClient(
            "http://service.test",
            Timeouts(),
            RetryPolicy(),
            transport=httpx.MockTransport(handler),
        ) as client:
            result = client.mutate(_pending(), lambda message: None)
        assert result.kind is expected
        assert calls == 1


def test_read_only_request_has_one_attempt() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    with BankingHttpClient(
        "http://service.test",
        Timeouts(),
        RetryPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.read("/requests")

    assert result.kind is ResultKind.TECHNICAL_ERROR
    assert calls == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["create-account", "--name", "Ali"],
        ["deposit", "--account-id", "1", "--amount", "10"],
        ["withdraw", "--account-id", "1", "--amount", "10"],
        ["balance", "--account-id", "1"],
        ["close-account", "--account-id", "1"],
        ["request", "get", str(uuid4())],
        ["request", "list", "--limit", "20"],
        ["pending", "list"],
        ["pending", "recover"],
        ["pending", "check", str(uuid4())],
        ["pending", "retry", str(uuid4()), "--confirm"],
    ],
)
def test_all_required_commands_parse(arguments) -> None:
    assert cli.build_parser().parse_args(arguments).command


def test_mutation_without_request_id_generates_uuid4() -> None:
    args = cli.build_parser().parse_args(
        ["deposit", "--account-id", "1", "--amount", "5"]
    )

    pending = cli._new_pending(args, "standalone")

    assert pending.request_id.version == 4


@pytest.mark.parametrize(
    "arguments",
    [
        ["deposit", "--account-id", "0", "--amount", "1"],
        ["withdraw", "--account-id", "1", "--amount", "0"],
        ["request", "list", "--limit", "101"],
        ["pending", "retry", str(uuid4())],
    ],
)
def test_invalid_cli_input_returns_client_error(
    tmp_path, arguments, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda path: _config(tmp_path))
    assert cli.main(arguments) == cli.ExitCode.CLIENT_ERROR


def test_cli_json_mutation_persists_before_send_and_removes_after_success(
    tmp_path, monkeypatch, capsys
) -> None:
    config = _config(tmp_path)
    request_id = uuid4()
    observed_pending = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def mutate(self, pending, progress):
            observed_pending.extend(PendingStore(config.pending_dir).list())
            return cli.ClientResult(
                ResultKind.SUCCESS, 200, {"status": "SUCCEEDED"}, "Success."
            )

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "BankingHttpClient", FakeClient)

    exit_code = cli.main(
        [
            "--json",
            "deposit",
            "--account-id",
            "7",
            "--amount",
            "10",
            "--request-id",
            str(request_id),
        ]
    )

    output = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert json.loads(output.out)["request_id"] == str(request_id)
    assert output.err == ""
    assert observed_pending[0].request_id == request_id
    assert PendingStore(config.pending_dir).list() == []


def test_cli_unknown_preserves_pending_and_uses_exit_three(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def mutate(self, pending, progress):
            return cli.ClientResult(ResultKind.UNKNOWN, 404, None, "Unknown.")

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "BankingHttpClient", FakeClient)

    assert cli.main(["create-account", "--name", "Ali"]) == cli.ExitCode.UNKNOWN
    assert len(PendingStore(config.pending_dir).list()) == 1


def test_cli_pending_uuid_collision_returns_four_without_network_send(
    tmp_path, monkeypatch, capsys
) -> None:
    config = _config(tmp_path)
    original = _pending()
    store = PendingStore(config.pending_dir)
    with store.locked():
        store.save(original)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        cli, "BankingHttpClient", lambda *args, **kwargs: calls.append("sent")
    )

    code = cli.main(
        [
            "withdraw",
            "--account-id",
            "7",
            "--amount",
            "99",
            "--request-id",
            str(original.request_id),
        ]
    )

    output = capsys.readouterr()
    assert code == cli.ExitCode.CLIENT_ERROR
    assert "pending check" in output.err
    assert calls == []
    assert store.get(original.request_id) == original


def test_pending_recover_only_looks_up_and_never_resends(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    pending = _pending()
    store = PendingStore(config.pending_dir)
    with store.locked():
        store.save(pending)

    class FakeClient:
        mutate_calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def lookup_pending(self, request_id):
            return cli.ClientResult(ResultKind.UNKNOWN, 404, None, "Unknown.")

        def mutate(self, pending, progress):
            type(self).mutate_calls += 1

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "BankingHttpClient", FakeClient)

    assert cli.main(["pending", "recover"]) == cli.ExitCode.UNKNOWN
    assert FakeClient.mutate_calls == 0
    assert store.get(pending.request_id) == pending


def test_pending_retry_reuses_stored_request_after_lookup(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    pending = _pending()
    store = PendingStore(config.pending_dir)
    with store.locked():
        store.save(pending)
    sent = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def lookup_pending(self, request_id):
            return cli.ClientResult(ResultKind.UNKNOWN, 404, None, "Unknown.")

        def mutate(self, stored, progress):
            sent.append(stored)
            return cli.ClientResult(ResultKind.SUCCESS, 200, {}, "Success.")

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "BankingHttpClient", FakeClient)

    code = cli.main(["pending", "retry", str(pending.request_id), "--confirm"])

    assert code == cli.ExitCode.SUCCESS
    assert sent == [pending]
    assert store.get(pending.request_id) is None


def test_pending_persistence_failure_prevents_network_send(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        cli.PendingStore,
        "save",
        lambda self, pending: (_ for _ in ()).throw(PendingStoreError("disk full")),
    )
    monkeypatch.setattr(
        cli, "BankingHttpClient", lambda *args, **kwargs: calls.append(1)
    )

    code = cli.main(["create-account", "--name", "Ali"])

    assert code == cli.ExitCode.TECHNICAL_ERROR
    assert calls == []
