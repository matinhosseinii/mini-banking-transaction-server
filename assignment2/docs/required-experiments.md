# Required Distributed Systems Experiments

Run commands from `assignment2/`. Start from the seeded Compose deployment in
the README, configure the Client for `http://127.0.0.1:8000`, and load the
Compose variables when SQL commands are used:

```sh
set -a
. infra/standalone/.env
set +a
COMPOSE="docker compose --env-file infra/standalone/.env -f infra/standalone/compose.yaml"
```

Use a fresh or known demo dataset. Capture terminal output, relevant JSON logs,
request UUIDs, before/after balances, and the verification SQL for the report.

## 1. Shard distribution and routing

Objective: prove exclusive range ownership and Gateway routing.

```sh
$COMPOSE exec -T postgres-a psql -U "$POSTGRES_A_USER" -d "$POSTGRES_A_DB" \
  -c "SELECT min(id), max(id), count(*) FROM accounts;" \
  -c "SELECT count(*) AS wrong_shard_rows FROM accounts WHERE id NOT BETWEEN 1 AND 1000;"
$COMPOSE exec -T postgres-b psql -U "$POSTGRES_B_USER" -d "$POSTGRES_B_DB" \
  -c "SELECT min(id), max(id), count(*) FROM accounts;" \
  -c "SELECT count(*) AS wrong_shard_rows FROM accounts WHERE id NOT BETWEEN 1001 AND 2000;"
.venv/bin/python -m client balance --account-id 1
.venv/bin/python -m client balance --account-id 1001
$COMPOSE logs --since 2m gateway | grep 'gateway_request_completed'
```

Expected: both `wrong_shard_rows` values are zero; Gateway log entries show
Shard A for account 1 and Shard B for account 1001. Capture both SQL result sets
and the two routing log entries.

## 2. Replica load distribution

Objective: show round-robin selection across both healthy replicas of Shard A.

```sh
for unused in $(seq 1 20); do
  curl -fsS http://127.0.0.1:8000/accounts/1/balance >/dev/null
done
$COMPOSE logs --since 2m gateway > /tmp/gateway-round-robin.log
grep -c '"replica_id":"bank-a1"' /tmp/gateway-round-robin.log
grep -c '"replica_id":"bank-a2"' /tmp/gateway-round-robin.log
```

Expected: both counts are nonzero and normally 10 each. Capture the counts and
several alternating log entries. No cleanup is required.

## 3. Concurrent operations through two replicas

Objective: prove shared PostgreSQL row locking prevents lost updates when both
replicas mutate account 1. Record the initial balance first.

```sh
.venv/bin/python -m client balance --account-id 1
$COMPOSE exec -T gateway python - <<'PY'
import concurrent.futures, uuid, httpx

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
.venv/bin/python -m client balance --account-id 1
```

Expected: twenty deposits and twenty withdrawals succeed and the final balance
equals the initial balance. Capture both balances and the forty HTTP statuses.
The database-backed equivalents are
`test_concurrent_deposits_do_not_lose_updates` and
`test_concurrent_withdrawals_never_make_balance_negative`.

## 4. Replica interruption under bounded load

Objective: stop `bank-a1`, observe continued service through `bank-a2`, then
observe `bank-a1` rejoin.

```sh
.venv/bin/python -m load_generator --scenario balance --account-id 1 \
  --requests 10000 --concurrency 5 --json > /tmp/interruption-load.json &
LOAD_PID=$!
$COMPOSE stop bank-a1
$COMPOSE logs --since 2m gateway | tail -50
$COMPOSE start bank-a1
wait "$LOAD_PID"
$COMPOSE logs --since 5m gateway > /tmp/interruption-gateway.log
grep -c '"replica_id":"bank-a2"' /tmp/interruption-gateway.log
grep -c '"replica_id":"bank-a1"' /tmp/interruption-gateway.log
```

Expected: after health detection, requests use `bank-a2`; after restart and the
configured health interval, new entries use `bank-a1` again. Some in-flight
requests may be UNKNOWN because the Gateway deliberately does not replay a
financial request after ambiguous delivery. Capture the load summary, service
state transitions, and timestamped selection logs.

## 5. Local and cross-shard transfers

Objective: contrast one-database atomic transfer with durable 2PC and verify
conservation. Capture balances for accounts 1, 2, and 1001 before and after.

```sh
LOCAL_ID=$(python -c 'import uuid; print(uuid.uuid4())')
CROSS_ID=$(python -c 'import uuid; print(uuid.uuid4())')
.venv/bin/python -m client balance --account-id 1
.venv/bin/python -m client balance --account-id 2
.venv/bin/python -m client balance --account-id 1001
.venv/bin/python -m client transfer --source-account-id 1 \
  --destination-account-id 2 --amount 10 --request-id "$LOCAL_ID"
.venv/bin/python -m client transfer --source-account-id 1 \
  --destination-account-id 1001 --amount 10 --request-id "$CROSS_ID"
.venv/bin/python -m client balance --account-id 1
.venv/bin/python -m client balance --account-id 2
.venv/bin/python -m client balance --account-id 1001
$COMPOSE exec -T postgres-coordinator psql -U "${POSTGRES_COORDINATOR_USER:-banking_coordinator_user}" \
  -d "${POSTGRES_COORDINATOR_DB:-banking_coordinator}" \
  -c "SELECT transaction_id,state,decision FROM coordinated_transfers WHERE transaction_id='$CROSS_ID';"
```

Expected: the local transfer changes accounts 1 and 2 atomically; the cross
transfer has `decision=COMMIT`, `state=RESOLVED`, and changes accounts 1 and
1001 by equal opposite amounts. Capture all balances and the coordinator row.

## 6. Coordinator interruption after durable COMMIT

Objective: terminate the Coordinator precisely after persisting COMMIT but
before sending either participant COMMIT, then prove restart recovery.

```sh
TX_ID=$(python -c 'import uuid; print(uuid.uuid4())')
export COORDINATOR_EXPERIMENT_CRASH_AFTER_COMMIT_TRANSACTION_ID="$TX_ID"
$COMPOSE up -d --force-recreate coordinator gateway
.venv/bin/python -m client transfer --source-account-id 1 \
  --destination-account-id 1001 --amount 10 --request-id "$TX_ID" || true
$COMPOSE logs --since 2m coordinator
$COMPOSE ps coordinator
.venv/bin/python -m client pending check "$TX_ID" || true
$COMPOSE exec -T postgres-coordinator psql -U "${POSTGRES_COORDINATOR_USER:-banking_coordinator_user}" \
  -d "${POSTGRES_COORDINATOR_DB:-banking_coordinator}" \
  -c "SELECT transaction_id,state,decision FROM coordinated_transfers WHERE transaction_id='$TX_ID';"
.venv/bin/python -m client balance --account-id 1
.venv/bin/python -m client balance --account-id 1001
$COMPOSE exec -T postgres-a psql -U "$POSTGRES_A_USER" -d "$POSTGRES_A_DB" \
  -c "SELECT gid FROM pg_prepared_xacts;"
$COMPOSE exec -T postgres-b psql -U "$POSTGRES_B_USER" -d "$POSTGRES_B_DB" \
  -c "SELECT gid FROM pg_prepared_xacts;"
```

Expected: logs contain `Experiment fault: exiting after durable COMMIT`; the
container restarts under Compose policy, startup recovery reuses `TX_ID`, the
row becomes `COMMIT/RESOLVED`, balances change once, and both prepared-xact
queries are empty. Capture the crash/restart logs, row, balances, and empty SQL
results.

Cleanup disables the hook for later demonstrations:

```sh
unset COORDINATOR_EXPERIMENT_CRASH_AFTER_COMMIT_TRANSACTION_ID
$COMPOSE up -d --force-recreate coordinator gateway
```

## 7. Concurrent overlapping cross-shard transfers

Objective: execute bounded unique transfers over the same source and
destination, poll durable outcomes, and verify exact balances and conservation.

```sh
.venv/bin/python scripts/cross_shard_experiment.py \
  --source 1 --destination 1001 --transfers 20 --amount 1 \
  --concurrency 5 --timeout 30 | tee /tmp/cross-shard-experiment.json
$COMPOSE exec -T postgres-coordinator psql -U "${POSTGRES_COORDINATOR_USER:-banking_coordinator_user}" \
  -d "${POSTGRES_COORDINATOR_DB:-banking_coordinator}" \
  -c "SELECT state,decision,count(*) FROM coordinated_transfers GROUP BY state,decision;"
$COMPOSE exec -T postgres-a psql -U "$POSTGRES_A_USER" -d "$POSTGRES_A_DB" \
  -c "SELECT gid FROM pg_prepared_xacts;"
$COMPOSE exec -T postgres-b psql -U "$POSTGRES_B_USER" -d "$POSTGRES_B_DB" \
  -c "SELECT gid FROM pg_prepared_xacts;"
```

Expected: script result is `PASS`, every listed UUID is `SUCCEEDED`, source
and destination deltas are equal and opposite, relevant coordinator rows are
resolved COMMITs, and both prepared-xact queries are empty. Capture the JSON
summary and SQL evidence.
