# Standalone Deployment

This guide deploys one Banking API and one PostgreSQL 17 database on Server A,
then runs the CLI and concurrent request generator from a separate Client VM.
Commands use Docker Compose only on Server A. They do not configure HA.

## Prerequisites

Server A needs Git, Docker Engine, and Docker Compose v2. The Client VM needs
Git and Python 3.12; Docker is not required there. Both VMs need Host-Only
network addresses, and TCP port 8000 on Server A must be reachable from the
Client VM. The examples use `192.168.56.11` for Server A; replace it with the
actual address.

Clone or update the repository on Server A and enter it:

```bash
git clone REPOSITORY_URL mini-banking-transaction-server
cd mini-banking-transaction-server
```

For an existing checkout, review local work before updating:

```bash
git status --short
git pull --ff-only
```

## Server A environment

Create the untracked Standalone environment file:

```bash
cp infra/standalone/.env.example infra/standalone/.env
chmod 600 infra/standalone/.env
```

Edit `infra/standalone/.env` and set a strong fictional database password and
the Host-Only bind address. A typical file is:

```dotenv
POSTGRES_DB=banking
POSTGRES_USER=banking_user
POSTGRES_PASSWORD=replace_with_a_strong_fictional_password
MAX_CONCURRENT_CLIENTS=100
LOG_LEVEL=INFO
UVICORN_LIMIT_CONCURRENCY=100
API_BIND_IP=192.168.56.11
```

Do not commit this file or print it during demonstrations. The PostgreSQL
database is stored in the Compose-managed `postgres-data` named volume.

## Build, migrate, optionally seed, and start

Run these steps in order after a fresh checkout or application update.

1. Build the API image:

   ```bash
   docker compose --env-file infra/standalone/.env \
     -f infra/standalone/compose.yaml build api
   ```

2. Start PostgreSQL and wait for its health check:

   ```bash
   docker compose --env-file infra/standalone/.env \
     -f infra/standalone/compose.yaml up -d --wait db
   ```

3. Apply pending Alembic revisions with a temporary API-image container:

   ```bash
   ./infra/standalone/scripts/migrate.sh
   ```

   The script resolves its own repository location, so it can be invoked from
   any working directory. It runs `alembic upgrade head`, removes the temporary
   container, and returns nonzero if migration fails. It is safe to repeat;
   Alembic applies only revisions not already recorded. It never starts the API
   or removes the PostgreSQL volume.

4. Optionally create three synthetic demo accounts:

   ```bash
   ./infra/standalone/scripts/seed.sh
   ```

   The seed creates Demo Accounts A, B, and C with balances 10000, 5000, and 0.
   Deterministic idempotency UUIDs make repeated and partially completed runs
   safe: accounts are not duplicated, deposits are not repeated, and existing
   balances are not reset. The concise output identifies account IDs and marks
   create/deposit operations as `new`, `replayed`, or `not-required`.

5. Start or recreate the API with the newly built image:

   ```bash
   docker compose --env-file infra/standalone/.env \
     -f infra/standalone/compose.yaml up -d --no-deps --force-recreate api
   docker compose --env-file infra/standalone/.env \
     -f infra/standalone/compose.yaml ps
   ```

This is also the update procedure for an existing deployment. Do not run
`docker compose down -v`, delete the named volume, reset the database, or assume
it is empty. Review future migrations before applying them. In a future HA
deployment, migrations must run only against the valid writable Primary; this
Standalone script is not Patroni-aware.

## Server verification

Check the applied revision and application tables without displaying the
database connection string:

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml run --rm --no-deps api \
  alembic -c /app/server/alembic.ini current

docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml exec db \
  psql -U banking_user -d banking -c '\dt accounts' -c '\dt requests'
```

Verify readiness from Server A and inspect structured JSON API logs:

```bash
curl --fail --show-error http://192.168.56.11:8000/ready
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml logs --tail=20 api
```

Expected readiness is HTTP 200 with `{"status":"ready"}`. The `db` and `api`
services shown by `docker compose ps` should both be healthy. API request log
lines should be JSON objects.

## Client VM setup

On the separate Client VM, clone the same revision, create a virtual
environment, and install only the client requirements:

```bash
git clone REPOSITORY_URL mini-banking-transaction-server
cd mini-banking-transaction-server
python3.12 -m venv .venv
.venv/bin/python -m pip install -r client/requirements.txt
mkdir -p ~/.mini-banking
cp client/config.example.json ~/.mini-banking/config.json
chmod 700 ~/.mini-banking
chmod 600 ~/.mini-banking/config.json
```

Edit `~/.mini-banking/config.json`. Use Server A's Host-Only address, not
`localhost`, for the standalone service. Keep the future HA profile in the same
configuration:

```json
{
  "default_service": "standalone",
  "services": {
    "standalone": {"base_url": "http://192.168.56.11:8000"},
    "ha": {"base_url": "http://WITNESS_HAPROXY_ADDRESS:PORT"}
  },
  "pending_dir": "~/.mini-banking/pending",
  "timeouts": {"connect": 3, "read": 15, "write": 5, "pool": 3},
  "retry": {
    "max_attempts": 3,
    "deadline_seconds": 90,
    "lookup_timeout_seconds": 10,
    "backoff_seconds": [5, 15],
    "jitter_seconds": 1
  }
}
```

Check network connectivity and the installed CLI:

```bash
curl --fail --show-error http://192.168.56.11:8000/ready
.venv/bin/python -m client --help
```

## CLI verification

Create an account and copy its returned `account_id` into the following
commands (the examples use 4):

```bash
.venv/bin/python -m client --service standalone \
  create-account --name "Standalone Verification"
.venv/bin/python -m client --service standalone \
  deposit --account-id 4 --amount 1000
.venv/bin/python -m client --service standalone \
  withdraw --account-id 4 --amount 100
.venv/bin/python -m client --service standalone \
  balance --account-id 4
.venv/bin/python -m client --service standalone \
  request list --account-id 4 --limit 20
```

To verify idempotency replay, invoke the exact same mutation twice with one
UUID and unchanged arguments:

```bash
REQUEST_ID=550e8400-e29b-41d4-a716-446655440000
.venv/bin/python -m client --service standalone \
  deposit --account-id 4 --amount 10 --request-id "$REQUEST_ID"
.venv/bin/python -m client --service standalone \
  deposit --account-id 4 --amount 10 --request-id "$REQUEST_ID"
```

The second result should contain `replayed: true`, and the balance should rise
only once. Also verify a confirmed business rejection without changing state:

```bash
.venv/bin/python -m client --service standalone \
  withdraw --account-id 4 --amount 999999999
```

## Concurrent withdrawal verification

Use a small workload appropriate for the VMs. Ensure the chosen account has
sufficient funds, then run:

```bash
.venv/bin/python -m load_generator \
  --service standalone \
  --scenario withdraw \
  --account-id 4 \
  --amount 10 \
  --requests 20 \
  --concurrency 5
```

Confirm that all 20 logical operations are classified, the actual balance
never becomes negative, and consistency is `PASS` when expected and actual
balances match with no `UNKNOWN` operations. `FAIL` means a trusted mismatch.
`INCONCLUSIVE` means an unknown mutation or failed balance read prevents a
claim. In particular, an HTTP 503 or transport failure must not be counted as a
successful withdrawal.

This verification assumes no other client changes the same account during the
run.

## Persistence verification

Record an account balance and request ID, stop the existing containers without
removing them or their volume, then start them in dependency order:

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml stop api db
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml up -d --wait db
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml up -d --no-deps api
```

From the Client VM, query the same account and request again:

```bash
.venv/bin/python -m client --service standalone balance --account-id 4
.venv/bin/python -m client --service standalone request get REQUEST_UUID
```

The balance and request history should remain available because the named
PostgreSQL volume was retained.

## Troubleshooting

- If PostgreSQL is not healthy, inspect `docker compose ... logs db` and verify
  the `.env` values. Do not paste secrets into issue reports.
- If migration or seed reports a connection failure, confirm `db` is healthy
  and that the temporary container joins the existing `banking` network.
- If the API is unhealthy, inspect `docker compose ... logs api`, confirm the
  migration reached `head`, and call `/ready` from Server A.
- If the Client VM cannot connect, verify the Host-Only IP, Server A firewall,
  port binding, and that the configuration does not use `localhost`.
- `UNKNOWN` means the mutation outcome is uncertain. Keep its UUID and use the
  client's request lookup/recovery commands; do not assume failure and resend
  it with a new UUID.

## Verification checklist

- [ ] PostgreSQL container is healthy.
- [ ] API container is healthy and `/ready` returns HTTP 200.
- [ ] Alembic reports the head revision.
- [ ] The `accounts` and `requests` tables exist.
- [ ] API request logs are structured JSON.
- [ ] The Client VM reaches Server A over HTTP.
- [ ] Create, deposit, withdrawal, balance, and request history work.
- [ ] An exact repeated idempotency request replays without a second mutation.
- [ ] Business errors are reported without corrupting balances.
- [ ] A concurrent run reports `PASS`, `FAIL`, or `INCONCLUSIVE` correctly.
- [ ] No account balance becomes negative.
- [ ] Balances and request history survive a safe container restart.

## Safe shutdown

Stop the containers while retaining all data:

```bash
docker compose --env-file infra/standalone/.env \
  -f infra/standalone/compose.yaml stop
```

Never add `-v` to a teardown command unless permanent database deletion is
explicitly intended and separately approved.
