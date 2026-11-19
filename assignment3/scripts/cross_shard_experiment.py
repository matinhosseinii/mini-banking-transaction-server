"""Run a bounded concurrent cross-shard transfer experiment."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from uuid import UUID, uuid4

import httpx


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--source", type=int, required=True)
    parser.add_argument("--destination", type=int, required=True)
    parser.add_argument("--transfers", type=int, default=20)
    parser.add_argument("--amount", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    request_ids = [uuid4() for _ in range(args.transfers)]
    semaphore = asyncio.Semaphore(args.concurrency)
    timeout = httpx.Timeout(args.timeout)

    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout) as client:
        source_before, destination_before = await asyncio.gather(
            _balance(client, args.source),
            _balance(client, args.destination),
        )

        async def submit(request_id: UUID) -> None:
            async with semaphore:
                try:
                    await client.post(
                        "/transfers",
                        json={
                            "source_account_id": args.source,
                            "destination_account_id": args.destination,
                            "amount": args.amount,
                        },
                        headers={"Idempotency-Key": str(request_id)},
                    )
                except httpx.RequestError:
                    return

        await asyncio.gather(*(submit(request_id) for request_id in request_ids))
        statuses = await _wait_for_outcomes(client, request_ids, args.timeout)
        source_after, destination_after = await asyncio.gather(
            _balance(client, args.source),
            _balance(client, args.destination),
        )

    succeeded = sum(status == "SUCCEEDED" for status in statuses.values())
    expected_source = source_before - succeeded * args.amount
    expected_destination = destination_before + succeeded * args.amount
    passed = (
        len(statuses) == len(request_ids)
        and all(status == "SUCCEEDED" for status in statuses.values())
        and source_after == expected_source
        and destination_after == expected_destination
        and source_before + destination_before == source_after + destination_after
    )
    print(
        json.dumps(
            {
                "request_ids": [str(item) for item in request_ids],
                "statuses": statuses,
                "source_before": source_before,
                "source_after": source_after,
                "destination_before": destination_before,
                "destination_after": destination_after,
                "conservation": source_before + destination_before
                == source_after + destination_after,
                "result": "PASS" if passed else "FAIL",
            },
            indent=2,
        )
    )
    return 0 if passed else 1


async def _balance(client: httpx.AsyncClient, account_id: int) -> int:
    response = await client.get(f"/accounts/{account_id}/balance")
    response.raise_for_status()
    return response.json()["balance"]


async def _wait_for_outcomes(
    client: httpx.AsyncClient, request_ids: list[UUID], timeout_seconds: float
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    pending = {str(item) for item in request_ids}
    outcomes: dict[str, str] = {}
    while pending and time.monotonic() < deadline:
        for request_id in tuple(pending):
            try:
                response = await client.get(f"/requests/{request_id}")
            except httpx.RequestError:
                continue
            if response.status_code == 200:
                status = response.json().get("status")
                if status in {"SUCCEEDED", "REJECTED"}:
                    outcomes[request_id] = status
                    pending.remove(request_id)
        if pending:
            await asyncio.sleep(0.2)
    return outcomes


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
