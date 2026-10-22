"""Command-only argparse interface for the Mini Banking client."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from enum import IntEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from client.config import DEFAULT_CONFIG_PATH, ClientConfig, ConfigError, load_config
from client.http import BankingHttpClient, ClientResult, ResultKind
from client.pending import (
    PendingCollisionError,
    PendingRequest,
    PendingStore,
    PendingStoreError,
)


class ExitCode(IntEnum):
    SUCCESS = 0
    GENERAL_ERROR = 1
    REJECTED = 2
    UNKNOWN = 3
    CLIENT_ERROR = 4
    TECHNICAL_ERROR = 5


class CliUsageError(ValueError):
    """Raised for argparse usage errors so the CLI can return code 4."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="python -m client")
    parser.add_argument("--service", help="configured service profile")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="configuration path"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON on stdout")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-account")
    create.add_argument("--name", required=True, type=_account_name)
    _request_id(create)
    for name in ("deposit", "withdraw"):
        mutation = commands.add_parser(name)
        mutation.add_argument("--account-id", required=True, type=_positive_int)
        mutation.add_argument("--amount", required=True, type=_positive_int)
        _request_id(mutation)
    balance = commands.add_parser("balance")
    balance.add_argument("--account-id", required=True, type=_positive_int)
    close = commands.add_parser("close-account")
    close.add_argument("--account-id", required=True, type=_positive_int)
    _request_id(close)

    request = commands.add_parser("request")
    request_commands = request.add_subparsers(dest="request_command", required=True)
    request_get = request_commands.add_parser("get")
    request_get.add_argument("request_id", type=_uuid)
    request_list = request_commands.add_parser("list")
    request_list.add_argument("--account-id", type=_positive_int)
    request_list.add_argument("--status", choices=("SUCCEEDED", "REJECTED"))
    request_list.add_argument(
        "--operation",
        choices=("CREATE_ACCOUNT", "CLOSE_ACCOUNT", "DEPOSIT", "WITHDRAW"),
    )
    request_list.add_argument("--limit", type=_limit, default=50)
    request_list.add_argument("--offset", type=_nonnegative_int, default=0)

    pending = commands.add_parser("pending")
    pending_commands = pending.add_subparsers(dest="pending_command", required=True)
    pending_commands.add_parser("list")
    pending_commands.add_parser("recover")
    pending_check = pending_commands.add_parser("check")
    pending_check.add_argument("request_id", type=_uuid)
    pending_retry = pending_commands.add_parser("retry")
    pending_retry.add_argument("request_id", type=_uuid)
    pending_retry.add_argument("--confirm", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        config = load_config(args.config)
        return int(_execute(args, config))
    except CliUsageError as error:
        print(f"Client input error: {error}", file=sys.stderr)
        return ExitCode.CLIENT_ERROR
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return ExitCode.CLIENT_ERROR
    except PendingCollisionError as error:
        print(f"Client input error: {error}", file=sys.stderr)
        return ExitCode.CLIENT_ERROR
    except PendingStoreError as error:
        print(f"Pending store error: {error}", file=sys.stderr)
        return ExitCode.TECHNICAL_ERROR
    except Exception:  # noqa: BLE001 - CLI boundary maps unclassified failures safely.
        print("Unexpected client failure.", file=sys.stderr)
        return ExitCode.GENERAL_ERROR


def _execute(args: argparse.Namespace, config: ClientConfig) -> ExitCode:
    if args.command == "pending":
        return _pending_command(args, config)
    service, base_url = config.base_url(args.service)
    if args.command in {"create-account", "deposit", "withdraw", "close-account"}:
        pending = _new_pending(args, service)
        store = PendingStore(config.pending_dir)
        with store.locked():
            store.save(pending)
            with BankingHttpClient(base_url, config.timeouts, config.retry) as client:
                result = client.mutate(pending, _progress)
            if result.definitive:
                store.remove(pending)
        return _emit_result(result, args.json, request_id=pending.request_id)
    with BankingHttpClient(base_url, config.timeouts, config.retry) as client:
        result = _read_command(args, client)
    return _emit_result(result, args.json)


def _new_pending(args: argparse.Namespace, service: str) -> PendingRequest:
    request_id = args.request_id or uuid4()
    if args.command == "create-account":
        operation, method, path, payload = (
            "CREATE_ACCOUNT",
            "POST",
            "/accounts",
            {"name": args.name},
        )
    elif args.command == "close-account":
        operation, method, path, payload = (
            "CLOSE_ACCOUNT",
            "DELETE",
            f"/accounts/{args.account_id}",
            None,
        )
    else:
        operation = args.command.upper()
        method = "POST"
        path = f"/accounts/{args.account_id}/{args.command}"
        payload = {"amount": args.amount}
    return PendingRequest.create(
        request_id=request_id,
        service=service,
        operation=operation,
        method=method,
        path=path,
        payload=payload,
    )


def _read_command(args: argparse.Namespace, client: BankingHttpClient) -> ClientResult:
    if args.command == "balance":
        return client.read(f"/accounts/{args.account_id}/balance")
    if args.request_command == "get":
        return client.read(f"/requests/{args.request_id}")
    params = {
        key: value
        for key, value in {
            "account_id": args.account_id,
            "status": args.status,
            "operation": args.operation,
            "limit": args.limit,
            "offset": args.offset,
        }.items()
        if value is not None
    }
    return client.read("/requests", params=params)


def _pending_command(args: argparse.Namespace, config: ClientConfig) -> ExitCode:
    store = PendingStore(config.pending_dir)
    with store.locked():
        if args.pending_command == "list":
            items = [_pending_document(item) for item in store.list()]
            _emit({"items": items, "total": len(items)}, args.json)
            return ExitCode.SUCCESS
        if args.pending_command in {"check", "retry"}:
            pending = store.get(args.request_id)
            if pending is None:
                _emit_error("Pending request not found.", args.json)
                return ExitCode.CLIENT_ERROR
            result = _lookup_for(config, pending)
            if result.definitive:
                store.remove(pending)
                return _emit_result(result, args.json, request_id=pending.request_id)
            if args.pending_command == "check":
                return _emit_result(result, args.json, request_id=pending.request_id)
            _, base_url = config.base_url(pending.service)
            with BankingHttpClient(base_url, config.timeouts, config.retry) as client:
                result = client.mutate(pending, _progress)
            if result.definitive:
                store.remove(pending)
            return _emit_result(result, args.json, request_id=pending.request_id)

        results = []
        unresolved = False
        for pending in store.list():
            result = _lookup_for(config, pending)
            if result.definitive:
                store.remove(pending)
            else:
                unresolved = True
            results.append(
                {
                    "request_id": str(pending.request_id),
                    "service": pending.service,
                    "result": result.kind.value,
                }
            )
        _emit({"items": results, "unresolved": unresolved}, args.json)
        return ExitCode.UNKNOWN if unresolved else ExitCode.SUCCESS


def _lookup_for(config: ClientConfig, pending: PendingRequest) -> ClientResult:
    _, base_url = config.base_url(pending.service)
    with BankingHttpClient(base_url, config.timeouts, config.retry) as client:
        return client.lookup_pending(pending)


def _emit_result(
    result: ClientResult, json_mode: bool, *, request_id: UUID | None = None
) -> ExitCode:
    document: dict[str, Any] = {
        "result": result.kind.value,
        "status_code": result.status_code,
        "detail": result.detail,
    }
    if request_id is not None:
        document["request_id"] = str(request_id)
    if result.data is not None:
        document["data"] = result.data
    _emit(document, json_mode)
    return {
        ResultKind.SUCCESS: ExitCode.SUCCESS,
        ResultKind.REJECTED: ExitCode.REJECTED,
        ResultKind.UNKNOWN: ExitCode.UNKNOWN,
        ResultKind.CLIENT_ERROR: ExitCode.CLIENT_ERROR,
        ResultKind.TECHNICAL_ERROR: ExitCode.TECHNICAL_ERROR,
    }[result.kind]


def _emit(document: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
        return
    for key, value in document.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, indent=2, sort_keys=True)}")
        else:
            print(f"{key}: {value}")


def _emit_error(detail: str, json_mode: bool) -> None:
    if json_mode:
        _emit({"result": "CLIENT_ERROR", "detail": detail}, True)
    else:
        print(detail, file=sys.stderr)


def _pending_document(request: PendingRequest) -> dict[str, Any]:
    document = asdict(request)
    document["request_id"] = str(request.request_id)
    return document


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


def _request_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request-id", type=_uuid)


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a valid UUID") from None


def _positive_int(value: str) -> int:
    parsed = _integer(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _account_name(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must contain non-whitespace")
    if len(value) > 100:
        raise argparse.ArgumentTypeError("must not exceed 100 characters")
    return value


def _nonnegative_int(value: str) -> int:
    parsed = _integer(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("must not exceed 100")
    return parsed


def _integer(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
