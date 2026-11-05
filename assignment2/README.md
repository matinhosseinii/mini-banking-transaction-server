# Distributed Banking Platform

Assignment 2 extends the banking system into a distributed platform. The
current implementation has two independent banking shards, each with one API
application and its own persistent PostgreSQL database.

| Shard | Canonical account IDs | Internal IDs | Direct API |
|---|---|---|---|
| A | `A0001`–`A1000` | 1–1000 | port 8001 |
| B | `A1001`–`A2000` | 1001–2000 | port 8002 |

The authoritative mapping is stored in [`server/shard_map.json`](server/shard_map.json).
Application instances enforce the same mapping before performing banking
operations, while each database also has a local account-range constraint.

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

Then build and start both databases:

```sh
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml up -d --build --wait postgres-a postgres-b
./infra/standalone/scripts/migrate.sh
./infra/standalone/scripts/seed.sh
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml up -d --wait bank-a bank-b
```

The migration script applies the existing Alembic schema independently to both
databases, installs each database's account-range constraint, and aligns its
identity sequence. The seed script is idempotent and creates three demo
accounts in each shard. Named volumes `postgres-a-data` and `postgres-b-data`
preserve balances across application container restarts.

With `API_BIND_IP=127.0.0.1`, test the shards directly at
`http://127.0.0.1:8001` and `http://127.0.0.1:8002`. For example, account 1 is
valid only on port 8001 and account 1001 only on port 8002. The current HTTP API
continues to use internal integer IDs; canonical `A0001` identifiers are
supported by the reusable shard resolver for future Gateway routing.

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

The Gateway, multiple application replicas per shard, transfers, the
Transaction Coordinator, and two-phase commit are not implemented yet. The two
shard applications are exposed directly only for development and validation.
