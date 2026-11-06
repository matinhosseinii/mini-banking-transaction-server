# Required Distributed Systems Experiments

Run commands from `assignment2/` after starting and seeding the Docker Compose deployment.

Configure the Client for `http://127.0.0.1:8000` and load environment variables:

```bash
set -a
. infra/standalone/.env
set +a

COMPOSE="docker compose --env-file infra/standalone/.env -f infra/standalone/compose.yaml"
```

Use a known demo dataset. Capture relevant logs, transaction IDs, SQL results, and before/after balances for the report and video.

## 1. Shard Distribution and Routing

Verify account ownership and Gateway routing:

```bash
$COMPOSE exec -T postgres-a psql -U "$POSTGRES_A_USER" -d "$POSTGRES_A_DB" \
  -c "SELECT min(id), max(id), count(*) FROM accounts;" \
  -c "SELECT count(*) AS wrong_shard_rows FROM accounts WHERE id NOT BETWEEN 1 AND 1000;"

$COMPOSE exec -T postgres-b psql -U "$POSTGRES_B_USER" -d "$POSTGRES_B_DB" \
  -c "SELECT min(id), max(id), count(*) FROM accounts;" \
  -c "SELECT count(*) AS wrong_shard_rows FROM accounts WHERE id NOT BETWEEN 1001 AND 2000;"

../.venv/bin/python -m client balance --account-id 1
../.venv/bin/python -m client balance --account-id 1001

$COMPOSE logs --since 2m gateway | grep 'gateway_request_completed'
```

**Expected:** Both `wrong_shard_rows` values are zero. Gateway logs identify Shard A for account 1 and Shard B for account 1001.

## 2. Replica Load Distribution

Send 20 requests to Shard A:

```bash
for unused in $(seq 1 20); do
  curl -fsS http://127.0.0.1:8000/accounts/1/balance >/dev/null
done

$COMPOSE logs --since 2m gateway > /tmp/gateway-round-robin.log

grep -c '"replica_id":"bank-a1"' /tmp/gateway-round-robin.log
grep -c '"replica_id":"bank-a2"' /tmp/gateway-round-robin.log
```

**Expected:** Both replicas receive requests, normally close to 10 each. Counts may include other requests in the log window.

## 3. Concurrent Operations Through Two Replicas

Record the initial balance:

```bash
../.venv/bin/python -m client balance --account-id 1
```

Execute concurrent deposits and withdrawals directly against the two replicas through the private Compose network:

```bash
$COMPOSE exec -T gateway python - <<'PY'
import concurrent.futures
import uuid
import httpx

def mutate(index):
    operation = "deposit" if index % 2 == 0 else "withdraw"
    replica = "bank-a1" if index % 2 == 0 else "bank-a2"

    return httpx.post(
        f"http://{replica}:8000/accounts/1/{operation}",
        json={"amount": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
        timeout=10,
    ).status_code

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
    print(list(pool.map(mutate, range(40))))
PY

../.venv/bin/python -m client balance --account-id 1
```

**Expected:** Twenty deposits and twenty withdrawals succeed, with the final balance equal to the initial balance. PostgreSQL row-level locking prevents lost updates.

## 4. Replica Interruption Under Load

Start bounded balance-request load in the background:

```bash
(
  for unused in $(seq 1 1000); do
    curl -sS -o /dev/null \
      http://127.0.0.1:8000/accounts/1/balance || true
  done
) > /tmp/interruption-load.log 2>&1 &

LOAD_PID=$!
```

Stop one replica while load is running:

```bash
$COMPOSE stop bank-a1
$COMPOSE logs --since 2m gateway | tail -30
```

Restart it:

```bash
$COMPOSE start bank-a1
wait "$LOAD_PID"
```

Inspect routing:

```bash
$COMPOSE logs --since 5m gateway > /tmp/interruption-gateway.log

grep -c '"replica_id":"bank-a1"' /tmp/interruption-gateway.log
grep -c '"replica_id":"bank-a2"' /tmp/interruption-gateway.log
```

**Expected:** The Gateway routes new requests through `bank-a2` while `bank-a1` is unavailable, then resumes selecting `bank-a1` after recovery.

Verify the timing using Gateway logs; aggregate counts alone do not prove failover.

## 5. Same-Shard and Cross-Shard Transfers

Generate unique request IDs:

```bash
LOCAL_ID=$(../.venv/bin/python -c 'import uuid; print(uuid.uuid4())')
CROSS_ID=$(../.venv/bin/python -c 'import uuid; print(uuid.uuid4())')
```

Record initial balances:

```bash
for id in 1 2 1001; do
  ../.venv/bin/python -m client balance --account-id "$id"
done
```

Execute both transfers:

```bash
../.venv/bin/python -m client transfer \
  --source-account-id 1 --destination-account-id 2 \
  --amount 10 --request-id "$LOCAL_ID"

../.venv/bin/python -m client transfer \
  --source-account-id 1 --destination-account-id 1001 \
  --amount 10 --request-id "$CROSS_ID"
```

Verify final balances:

```bash
for id in 1 2 1001; do
  ../.venv/bin/python -m client balance --account-id "$id"
done
```

Inspect the Coordinator decision:

```bash
$COMPOSE exec -T postgres-coordinator \
  psql -U "${POSTGRES_COORDINATOR_USER:-banking_coordinator_user}" \
  -d "${POSTGRES_COORDINATOR_DB:-banking_coordinator}" \
  -c "SELECT transaction_id, state, decision FROM coordinated_transfers WHERE transaction_id='$CROSS_ID';"
```

**Expected:** Both transfers preserve the total balance. The cross-shard transfer reaches `COMMIT/RESOLVED`.

## 6. Coordinator Crash After Durable COMMIT

This experiment terminates the Coordinator after persisting COMMIT but before sending participant COMMIT messages.

Ensure the Coordinator image contains the fault-injection implementation. Rebuild it first if the image is outdated.

Record initial balances, then generate a new transaction ID:

```bash
TX_ID=$(../.venv/bin/python -c 'import uuid; print(uuid.uuid4())')
export COORDINATOR_EXPERIMENT_CRASH_AFTER_COMMIT_TRANSACTION_ID="$TX_ID"
echo "$TX_ID"
```

Recreate the Coordinator with the experiment setting:

```bash
$COMPOSE up -d --force-recreate coordinator

$COMPOSE exec coordinator \
  printenv COORDINATOR_EXPERIMENT_CRASH_AFTER_COMMIT_TRANSACTION_ID
```

Execute the transfer once:

```bash
../.venv/bin/python -m client --json transfer \
  --source-account-id 1 --destination-account-id 1001 \
  --amount 10 --request-id "$TX_ID"
```

The Client may retry with the same UUID during recovery. Do not create a second transfer with a new UUID.

Verify the crash and automatic restart:

```bash
docker inspect assignment2-gateway-coordinator-1 \
  --format 'RestartCount={{.RestartCount}} Status={{.State.Status}}'

$COMPOSE logs --since 3m coordinator
```

Expected log:

```text
Experiment fault: exiting after durable COMMIT
```

Verify the durable decision:

```bash
$COMPOSE exec -T postgres-coordinator \
  psql -U "${POSTGRES_COORDINATOR_USER:-banking_coordinator_user}" \
  -d "${POSTGRES_COORDINATOR_DB:-banking_coordinator}" \
  -c "SELECT transaction_id, state, decision FROM coordinated_transfers WHERE transaction_id='$TX_ID';"
```

Verify balances and prepared transactions:

```bash
../.venv/bin/python -m client balance --account-id 1
../.venv/bin/python -m client balance --account-id 1001

$COMPOSE exec -T postgres-a \
  psql -U "$POSTGRES_A_USER" -d "$POSTGRES_A_DB" \
  -c "SELECT gid FROM pg_prepared_xacts;"

$COMPOSE exec -T postgres-b \
  psql -U "$POSTGRES_B_USER" -d "$POSTGRES_B_DB" \
  -c "SELECT gid FROM pg_prepared_xacts;"
```

**Expected:** Coordinator restarts, the transaction reaches `COMMIT/RESOLVED`, both balances change exactly once, and no prepared transactions remain.

Disable fault injection afterward:

```bash
unset COORDINATOR_EXPERIMENT_CRASH_AFTER_COMMIT_TRANSACTION_ID
$COMPOSE up -d --force-recreate coordinator
```

## 7. Concurrent Cross-Shard Transfers

Run 20 overlapping transfers with five concurrent workers:

```bash
../.venv/bin/python scripts/cross_shard_experiment.py \
  --source 1 --destination 1001 \
  --transfers 20 --amount 1 \
  --concurrency 5 --timeout 30 \
  | tee /tmp/cross-shard-experiment.json
```

Inspect Coordinator states:

```bash
$COMPOSE exec -T postgres-coordinator \
  psql -U "${POSTGRES_COORDINATOR_USER:-banking_coordinator_user}" \
  -d "${POSTGRES_COORDINATOR_DB:-banking_coordinator}" \
  -c "SELECT state, decision, count(*) FROM coordinated_transfers GROUP BY state, decision;"
```

Verify no unresolved prepared transactions remain:

```bash
$COMPOSE exec -T postgres-a \
  psql -U "$POSTGRES_A_USER" -d "$POSTGRES_A_DB" \
  -c "SELECT gid FROM pg_prepared_xacts;"

$COMPOSE exec -T postgres-b \
  psql -U "$POSTGRES_B_USER" -d "$POSTGRES_B_DB" \
  -c "SELECT gid FROM pg_prepared_xacts;"
```

**Expected:** The script reports `PASS`, all 20 transactions succeed, balances change by equal and opposite amounts, and no prepared transactions remain.

Capture the JSON summary and SQL results for the technical report and demo video.