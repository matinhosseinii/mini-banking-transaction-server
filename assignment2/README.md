# Distributed Banking Platform

Assignment 2 will extend the banking system into a distributed platform. The
current implementation is an independent baseline copied from Assignment 1 and
retains its existing single-server banking API, client, load generator,
PostgreSQL database, and Docker Compose deployment.

Sharding, application replication, the Gateway, the Transaction Coordinator,
and durable two-phase commit will be implemented in later commits.

## Install dependencies

Run these commands from the `assignment2/` directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

## Run the baseline with Docker Compose

Create the local deployment configuration and set suitable development values:

```sh
cp infra/standalone/.env.example infra/standalone/.env
```

Then build and start the database and API:

```sh
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml up -d --build --wait
./infra/standalone/scripts/migrate.sh
```

The API listens on the address configured by `API_BIND_IP` on port 8000.

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
