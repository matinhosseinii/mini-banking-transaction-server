# Concurrent Request Generator

The load generator is a small `asyncio` command-line tool for demonstrating
concurrent operations against one existing account. It uses one pooled
`httpx.AsyncClient`, a bounded set of workers, and an `asyncio.Semaphore`. It is
not intended to be a general-purpose load-testing framework.

## Installation and configuration

Create the project virtual environment and install the client dependency:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r client/requirements.txt
```

The generator reads the same JSON configuration as the command-only client.
By default this is `~/.mini-banking/config.json`; use `--config PATH` to select
another file. `--service standalone` uses the configured standalone URL, while
`--service ha` uses the HA profile, which should point to HAProxy on Witness.
The default service profile is used when `--service` is omitted. The generator
does not perform discovery or failover.

## Command syntax

```text
python -m load_generator --scenario {balance,deposit,withdraw,mixed}
    --account-id ID --requests COUNT --concurrency COUNT
    [--amount AMOUNT] [--service PROFILE] [--config PATH] [--json]
```

`--account-id`, `--requests`, `--concurrency`, and `--amount` accept positive
integers. `--amount` is required for `deposit`, `withdraw`, and `mixed`. The
target account must already exist; the tool never creates or seeds accounts.

The scenarios are:

- `balance`: concurrent balance inquiries.
- `deposit`: concurrent deposits of the specified amount.
- `withdraw`: concurrent withdrawals of the specified amount.
- `mixed`: alternating deposits and withdrawals, beginning with a deposit.

Each financial operation receives a distinct UUIDv4 idempotency key and is sent
exactly once. The concurrency value is the maximum number of in-flight logical
operations, not a number of unique clients.

## Examples

Concurrent withdrawals against standalone:

```bash
.venv/bin/python -m load_generator \
  --service standalone \
  --scenario withdraw \
  --account-id 1 \
  --amount 10 \
  --requests 100 \
  --concurrency 20
```

An alternating mixed run through HAProxy on Witness:

```bash
.venv/bin/python -m load_generator \
  --service ha \
  --scenario mixed \
  --account-id 1 \
  --amount 5 \
  --requests 40 \
  --concurrency 10 \
  --json
```

Default output is human-readable. `--json` keeps standard output to one JSON
document containing the summary and per-operation outcomes, including UUIDs for
all financial requests.

## Balance consistency

Financial scenarios read the balance before and after the concurrent work. The
expected final balance is:

```text
initial balance + confirmed successful deposits - confirmed successful withdrawals
```

Confirmed rejections do not affect the expected value. `PASS` means expected
and actual balances match and no operation is `UNKNOWN`; `FAIL` means they
differ and no operation is `UNKNOWN`; `INCONCLUSIVE` means an unknown mutation,
a failed balance read, or another condition prevents trustworthy verification.
The balance-only scenario does not perform this verification.

This check assumes no unrelated client changes the account during the run.

## UNKNOWN outcomes and limitations

A mutation is `UNKNOWN` after a timeout, connection failure, HTTP 5xx response
(including Uvicorn overload HTTP 503), or malformed/ambiguous success response.
The generator does not retry or look up the request. Retain the reported UUID
for later inspection with the normal client. A confirmed persisted business
rejection is `REJECTED`; read-only transport and server failures are `ERROR`.

Results are kept only for the current process. There is no pending store, fault
injection, percentile analysis, dashboard, HA discovery, or automated recovery.

## Uvicorn concurrency limit

Set `UVICORN_LIMIT_CONCURRENCY` to a positive integer to pass Uvicorn's
`limit_concurrency` setting. For example:

```text
UVICORN_LIMIT_CONCURRENCY=100
```

Leaving it unset (or empty in Compose) preserves Uvicorn's default behavior.
This limits concurrent connections/tasks; it does not count unique clients and
does not add Uvicorn workers.
