"""Command-line interface for the concurrent request generator."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from client.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from load_generator.core import RunResult, run_generator


class CliUsageError(ValueError):
    """Raised instead of terminating inside argparse."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="python -m load_generator")
    parser.add_argument(
        "--scenario",
        required=True,
        choices=("balance", "deposit", "withdraw", "mixed"),
    )
    parser.add_argument("--account-id", required=True, type=_positive_int)
    parser.add_argument("--amount", type=_positive_int)
    parser.add_argument("--requests", required=True, type=_positive_int)
    parser.add_argument("--concurrency", required=True, type=_positive_int)
    parser.add_argument("--service", help="configured service profile")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--json", action="store_true", help="emit JSON on stdout")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    if args.scenario != "balance" and args.amount is None:
        raise CliUsageError("--amount is required for financial scenarios")
    return args


def format_human(result: RunResult) -> str:
    lines = [
        f"Scenario:         {result.scenario}",
        f"Service:          {result.service}",
        f"Account ID:       {result.account_id}",
        f"Total Requests:   {result.total_requests}",
        f"Concurrency:      {result.concurrency}",
        "",
        f"Succeeded:        {result.succeeded}",
        f"Rejected:         {result.rejected}",
        f"Unknown:          {result.unknown}",
        f"Errors:           {result.errors}",
        f"Elapsed Seconds:  {result.elapsed_seconds:.3f}",
    ]
    if result.consistency is not None:
        lines.extend(
            [
                "",
                f"Initial Balance:  {_display(result.initial_balance)}",
                f"Expected Balance: {_display(result.expected_balance)}",
                f"Actual Balance:   {_display(result.actual_balance)}",
                "",
                f"Consistency:      {result.consistency}",
            ]
        )
    if result.unknown:
        ids = [
            item.request_id for item in result.operations if item.outcome == "UNKNOWN"
        ]
        lines.extend(["", "Unknown Request IDs:", *(f"  {item}" for item in ids)])
    return "\n".join(lines)


def _display(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        config = load_config(args.config)
        service, base_url = config.base_url(args.service)
        result = asyncio.run(
            run_generator(
                base_url=base_url,
                service=service,
                scenario=args.scenario,
                account_id=args.account_id,
                requests=args.requests,
                concurrency=args.concurrency,
                amount=args.amount,
                timeouts=config.timeouts,
            )
        )
    except (CliUsageError, ConfigError) as error:
        print(f"Load generator error: {error}", file=sys.stderr)
        return 2
    output = (
        json.dumps(result.to_dict(), separators=(",", ":"))
        if args.json
        else format_human(result)
    )
    print(output)
    return 0
