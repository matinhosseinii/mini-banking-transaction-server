# Assignment 2 — Distributed Banking Platform

A distributed banking platform implementing sharding, application replication, fault-tolerant routing, and distributed transactions using PostgreSQL Two-Phase Commit (2PC).

Built with Python, FastAPI, SQLAlchemy, PostgreSQL, and Docker Compose.

## Architecture

The platform runs in **9 Docker containers**:

| Component | Description |
|---|---|
| Gateway | Shard-aware routing, round-robin load balancing, health-aware failover |
| Shard A | Accounts `A0001–A1000`, two Banking replicas, PostgreSQL A |
| Shard B | Accounts `A1001–A2000`, two Banking replicas, PostgreSQL B |
| Coordinator | Durable 2PC decisions and crash recovery |
| Coordinator DB | Persistent distributed transaction journal |

Account ownership is defined in [`server/shard_map.json`](server/shard_map.json). Each shard has one authoritative PostgreSQL database shared by its two stateless Banking replicas.

Clients communicate exclusively through the Gateway.

## Banking Operations

All Assignment 1 banking functionality is preserved, including account creation, balance inquiry, deposit, withdrawal, account closure, and durable idempotency.

- **Same-shard transfers:** Atomic PostgreSQL transactions with deterministic row locking.
- **Cross-shard transfers:** Durable 2PC using PostgreSQL prepared transactions.
- **Concurrency:** Row-level locks and advisory locks prevent lost updates and duplicate effects.
- **Availability:** The Gateway routes requests to healthy replicas.
- **Recovery:** The Coordinator persists COMMIT/ABORT decisions and resolves interrupted transactions after restart.

Uncertain transaction outcomes remain queryable by request UUID.

## Deployment

Run commands from `assignment2/`.

### 1. Configure

```bash
cp infra/standalone/.env.example infra/standalone/.env
```

Edit database credentials and set `API_BIND_IP=127.0.0.1` for local deployment.

### 2. Start databases

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml \
  up -d --wait postgres-a postgres-b postgres-coordinator
```

### 3. Migrate and seed

```bash
./infra/standalone/scripts/migrate.sh
./infra/standalone/scripts/seed.sh
```

The Coordinator applies its own Alembic migration automatically at startup.

### 4. Build and start

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml up -d --build --wait
```

Verify all nine containers are healthy:

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml ps
```

Only the Gateway is exposed on the host (`127.0.0.1:8000` by default).

## Docker Hub Deployment

Prebuilt application images are available on Docker Hub:

| Component | Docker Hub Image |
|---|---|
| Banking Server (all four replicas) | [matinhosseinii/banking-server](https://hub.docker.com/r/matinhosseinii/banking-server) |
| Gateway | [matinhosseinii/banking-gateway](https://hub.docker.com/r/matinhosseinii/banking-gateway) |
| Coordinator | [matinhosseinii/banking-coordinator](https://hub.docker.com/r/matinhosseinii/banking-coordinator) |

All images use the `assignment2` tag. PostgreSQL uses the official `postgres:17` image.

The `compose.hub.yaml` override replaces locally built application images with the published images.

After configuring `.env` and initializing the shard databases using the Deployment instructions above, pull the images:

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml \
  -f infra/standalone/compose.hub.yaml pull
```

Start the complete platform without building locally:

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml \
  -f infra/standalone/compose.hub.yaml \
  up -d --no-build --wait
```

Verify all nine containers are healthy:

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml \
  -f infra/standalone/compose.hub.yaml ps
```

## Client Usage

Configure the Client to use the Gateway:

```bash
mkdir -p ~/.mini-banking
cp client/config.example.json ~/.mini-banking/config.json
sed -i 's|http://gateway.example:8000|http://127.0.0.1:8000|' \
  ~/.mini-banking/config.json
```

Examples (using the repository-root virtual environment):

```bash
../.venv/bin/python -m client balance --account-id 1

# Same-shard transfer
../.venv/bin/python -m client transfer \
  --source-account-id 1 --destination-account-id 2 --amount 100

# Cross-shard transfer
../.venv/bin/python -m client transfer \
  --source-account-id 1 --destination-account-id 1001 --amount 100
```

The Client manages request UUIDs, pending requests, retries, and status lookup.

## Tests and Required Experiments

Static checks:

```bash
../.venv/bin/ruff check .
../.venv/bin/ruff format --check .
```

Run tests:

```bash
../.venv/bin/python -m pytest
```

PostgreSQL integration tests require `TEST_POSTGRES_ADMIN_URL`. Participant tests also require `max_prepared_transactions > 0`.

All seven mandatory experiments, including replica failover, Coordinator crash recovery, and concurrent cross-shard transfers, are documented in:

**[Required Experiments](docs/required-experiments.md)**

## Limitations

- Single Coordinator instance; no Coordinator high availability.
- No PostgreSQL database replication or automatic database failover.
- Internal participant endpoints trust the private Docker network.
- Expert bonuses (live re-sharding and autonomous self-healing) are not implemented.

## Shutdown

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml down
```

Persistent database volumes are retained.