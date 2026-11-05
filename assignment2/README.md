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

## Current limitations

Transfers, the Transaction Coordinator, and two-phase commit are not
implemented yet. Request-history aggregation is limited to the first 100
matching records from each shard per Gateway request.
