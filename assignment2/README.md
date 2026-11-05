# Distributed Banking Platform

Assignment 2 extends the banking system into a distributed platform. The
current implementation has two independent banking shards, two stateless API
replicas per shard, and a shard-aware Gateway.

| Shard | Canonical account IDs | Internal IDs | Replicas |
|---|---|---|---|
| A | `A0001`–`A1000` | 1–1000 | `bank-a1`, `bank-a2` |
| B | `A1001`–`A2000` | 1001–2000 | `bank-b1`, `bank-b2` |

The authoritative mapping is stored in [`server/shard_map.json`](server/shard_map.json).
Application instances enforce the same mapping before performing banking
operations, while each database also has a local account-range constraint.
Both replicas of a shard use the same authoritative PostgreSQL database.

Clients send all requests to the Gateway on port 8000. Account-addressed
requests are routed with the shared Shard Map. Healthy replicas are selected by
independent per-shard in-memory round robin. Health checks periodically remove
failed replicas and return recovered replicas to rotation. If a selected
backend fails while handling a request, the Gateway returns 502 and does not
retry the request, avoiding unsafe mutation replay.

Account creation has no account ID to route. The Gateway hashes the required
`Idempotency-Key` deterministically to A or B, so retries always reach the same
shard without requiring the Client to know shard identities.

## Same-shard transfers

`POST /transfers` atomically moves a positive amount between two active
accounts owned by the same shard. Both rows are locked in ascending account-ID
order and both balance changes commit in one PostgreSQL transaction. The
durable `Idempotency-Key` record is committed in that transaction, so replaying
an identical request cannot transfer money twice.

The Client exposes the same operation through the Gateway:

```sh
.venv/bin/python -m client transfer \
  --source-account-id 1 --destination-account-id 2 --amount 100
```

Transfers between Shards A and B return
`CROSS_SHARD_TRANSFER_NOT_SUPPORTED`; public cross-shard coordination remains
for a later commit.

## Durable 2PC participants

Each Banking Server now exposes an internal participant API on the Compose
network:

- `POST /internal/2pc/prepare`
- `POST /internal/2pc/commit`
- `POST /internal/2pc/abort`
- `GET /internal/2pc/{transaction_id}`

PREPARE validates shard ownership and the active account, locks the account
row, applies one local `DEBIT` or `CREDIT`, and uses PostgreSQL `PREPARE
TRANSACTION`. The change and its row lock remain pending until COMMIT or ABORT.
These endpoints trust callers on the private Compose network; they are not
routed by the public Gateway and the banking replicas have no published host
ports.

The `participant_transactions` table records the transaction UUID, exact
payload, shard-specific prepared-transaction GID, and durable outcome marker.
A small `participant_abort_decisions` tombstone is written before rollback so
a crash cannot allow an aborted transaction to be prepared again. Identical
PREPARE, COMMIT, and ABORT messages are safe to repeat. Reusing a UUID with a
different payload is rejected. The balance change and `COMMITTED` marker become
visible atomically through `COMMIT PREPARED`; unresolved or ambiguous database
outcomes are reported as `UNKNOWN` rather than guessed. This state is shared by
both replicas of a shard and survives application restarts.

## Install dependencies

Run these commands from the `assignment2/` directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

## Run the two shards with Docker Compose

Create the local deployment configuration and set suitable development values:

```sh
cp infra/standalone/.env.example infra/standalone/.env
```

Then build and start both databases, initialize them, and launch the complete
deployment:

```sh
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml up -d --build --wait postgres-a postgres-b
./infra/standalone/scripts/migrate.sh
./infra/standalone/scripts/seed.sh
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml up -d --build --wait
```

The migration script applies the existing Alembic schema independently to both
databases, installs each database's account-range constraint, and aligns its
identity sequence. The seed script is idempotent and creates three demo
accounts in each shard. Named volumes `postgres-a-data` and `postgres-b-data`
preserve balances across application container restarts.

Both PostgreSQL services start with `max_prepared_transactions=20`. Changing
that server setting requires restarting the PostgreSQL containers, but does
not require deleting or recreating their persistent volumes.

With `API_BIND_IP=127.0.0.1`, configure the Client's sole service URL as
`http://127.0.0.1:8000`; it does not need replica or shard addresses. The
current HTTP API continues to use internal integer IDs.

Round-robin selection is visible in Gateway and banking replica logs. To
demonstrate failover and recovery, stop and restart one replica while sending
requests for its shard:

```sh
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml stop bank-a1
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml start bank-a1
```

After the configured health interval, `bank-a1` rejoins Shard A's rotation.

## Run checks and tests

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pip check
.venv/bin/python -m pytest
```

Database-backed integration tests require `TEST_POSTGRES_ADMIN_URL` to point to
an available PostgreSQL administrative database. Without it, those tests are
skipped.

The focused participant tests also require that PostgreSQL instance to have a
nonzero `max_prepared_transactions` value:

```sh
TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://... \
  .venv/bin/python -m pytest tests/integration/test_participant.py -q
```

## Current limitations

The Transaction Coordinator and public cross-shard transfer orchestration are
not implemented yet; the current release provides participant primitives only
and is not a complete distributed 2PC system. Request-history aggregation is
limited to the first 100 matching records from each shard per Gateway request.
