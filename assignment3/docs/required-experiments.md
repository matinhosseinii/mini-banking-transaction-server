# Required Kubernetes Failure Experiments

Run these commands manually from `assignment3/` against the dedicated Minikube demo
deployment. They intentionally delete Pods, crash the Coordinator, generate load, or
change Deployment images. They never delete PVCs and never resolve PostgreSQL prepared
transactions manually.

## Common prerequisites and evidence

Apply the reviewed application, Patroni, HPA, and optional NetworkPolicy manifests;
run shard migrations and seed dedicated active accounts first. Configure
`~/.mini-banking/config.json` so its Gateway URL is the Minikube NodePort, then set:

```sh
test -f k8s/applications.yaml
test -x ../.venv/bin/python
PYTHON=../.venv/bin/python
API_URL="http://$(minikube ip):30080"
ACCOUNT_A=1
ACCOUNT_B=1001
```

Metrics Server is additionally required for experiment 5. Experiments 1, 3, 5, and 6
reuse the existing bounded load generator. Experiment 3 also reuses
`scripts/cross_shard_experiment.py`.

Define this helper once and call it immediately before every database verification.
It prevents a stale Pod name after Patroni changes leadership:

```sh
refresh_primaries() {
  A_PRIMARY="$(kubectl get pods \
    -l 'application=patroni,cluster-name=postgres-a,role=primary' \
    -o jsonpath='{.items[0].metadata.name}')"
  B_PRIMARY="$(kubectl get pods \
    -l 'application=patroni,cluster-name=postgres-b,role=primary' \
    -o jsonpath='{.items[0].metadata.name}')"
  COORD_PRIMARY="$(kubectl get pods \
    -l 'application=patroni,cluster-name=postgres-coordinator,role=primary' \
    -o jsonpath='{.items[0].metadata.name}')"
  test -n "$A_PRIMARY" && test -n "$B_PRIMARY" && test -n "$COORD_PRIMARY"
}
refresh_primaries
```

Save these real database queries before and after each experiment:

```sh
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT id, name, balance, status FROM accounts WHERE id=$ACCOUNT_A;"
kubectl exec "$B_PRIMARY" -- psql -U postgres -d banking_b -c \
  "SELECT id, name, balance, status FROM accounts WHERE id=$ACCOUNT_B;"
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT request_id, operation, status, amount, balance_before, balance_after
     FROM requests ORDER BY created_at DESC LIMIT 20;"
kubectl exec "$COORD_PRIMARY" -- psql -U postgres -d banking_coordinator -c \
  "SELECT transaction_id, state, decision, source_state, destination_state
     FROM coordinated_transfers ORDER BY created_at DESC LIMIT 20;"
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT gid, prepared, owner, database FROM pg_prepared_xacts;"
kubectl exec "$B_PRIMARY" -- psql -U postgres -d banking_b -c \
  "SELECT gid, prepared, owner, database FROM pg_prepared_xacts;"
```

Do not interpret an HTTP timeout as failure. Preserve every request UUID and reconcile
it through `python -m client request get UUID` or the database rows.

## 1. Application Pod Failure

**Prerequisites and baseline:** Confirm at least two Ready Shard A Pods, record the
common account/request queries, and start bounded mixed load:

```sh
kubectl get deployment bank-a bank-b
kubectl get pods -l app=bank-a -o wide
"$PYTHON" -m load_generator --scenario mixed \
  --account-id "$ACCOUNT_A" --amount 1 --requests 400 --concurrency 20 --json \
  > /tmp/application-pod-failure.json &
LOAD_PID=$!
```

**Failure injection:** Delete one dynamically selected Banking Pod:

```sh
VICTIM="$(kubectl get pods -l app=bank-a \
  -o jsonpath='{.items[0].metadata.name}')"
echo "INTENTIONAL FAILURE: deleting $VICTIM"
kubectl delete pod "$VICTIM"
kubectl rollout status deployment/bank-a --timeout=120s
wait "$LOAD_PID" || true
```

**Expected observations and database checks:** Other Ready Pods continue serving new
connections and the Deployment creates a replacement. Reconcile any UNKNOWN UUIDs;
do not assume they failed. Rediscover the Primaries, rerun the common queries, and
verify every successful mutation has one `requests` row and a matching balance delta:

```sh
kubectl get pods -l app=bank-a -o wide
kubectl get endpointslice -l kubernetes.io/service-name=bank-a
refresh_primaries
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT operation, status, count(*) FROM requests
     WHERE requested_account_id=$ACCOUNT_A GROUP BY operation, status;"
```

**Cleanup:** No manual repair is needed. Confirm all Banking Pods are Ready and retain
the load JSON as evidence.

## 2. Primary Database Failure

**Prerequisites and baseline:** Require a healthy synchronous Standby. Record the
account, request rows, membership, replication state, and prepared transactions:

```sh
refresh_primaries
kubectl exec "$A_PRIMARY" -- patronictl -c /etc/patroni/patroni.yml list
kubectl exec "$A_PRIMARY" -- psql -U postgres -d postgres -c \
  "SELECT application_name, state, sync_state FROM pg_stat_replication;"
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT id, balance FROM accounts WHERE id=$ACCOUNT_A;" \
  -c "SELECT gid, prepared FROM pg_prepared_xacts;"
OLD_PRIMARY="$A_PRIMARY"
```

Proceed only when one replication row is `state=streaming` and `sync_state=sync`.

**Failure injection:** Delete the actual Primary Pod, never its PVC:

```sh
echo "INTENTIONAL FAILURE: deleting PostgreSQL Primary $OLD_PRIMARY"
kubectl delete pod "$OLD_PRIMARY"
kubectl get pods -l 'application=patroni,cluster-name=postgres-a' -L role --watch
```

Stop watching after election and replacement settle. Strict synchronous mode may make
writes unavailable until a synchronous Standby exists; do not weaken it.

**Expected observations and database checks:** Rediscover the Primary. Committed data
must survive. Exactly one Pod carries `role=primary`, only it is writable, and any
prepared GID remains identifiable until the durable Coordinator decision resolves it.

```sh
refresh_primaries
test "$(kubectl get pods \
  -l 'application=patroni,cluster-name=postgres-a,role=primary' \
  -o name | wc -l)" -eq 1
for pod in $(kubectl get pods \
  -l 'application=patroni,cluster-name=postgres-a' \
  -o jsonpath='{.items[*].metadata.name}'); do
  echo "$pod"
  kubectl exec "$pod" -- psql -U postgres -d postgres -Atc \
    "SELECT pg_is_in_recovery();"
done
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT id, balance FROM accounts WHERE id=$ACCOUNT_A;" \
  -c "SELECT request_id, operation, status FROM requests
        WHERE requested_account_id=$ACCOUNT_A ORDER BY created_at DESC LIMIT 20;" \
  -c "SELECT gid, prepared, owner, database FROM pg_prepared_xacts;"
```

**Cleanup:** Wait for two healthy members and synchronous replication. Never run
`COMMIT PREPARED` or `ROLLBACK PREPARED` manually.

## 3. Concurrent Transactions

**Prerequisites and baseline:** Use funded accounts on different shards. Record both
balances and ensure the debit account covers the requested total.

**Load injection:** Reuse the existing cross-shard checker and capture all UUIDs:

```sh
"$PYTHON" scripts/cross_shard_experiment.py \
  --base-url "$API_URL" --source "$ACCOUNT_A" --destination "$ACCOUNT_B" \
  --transfers 20 --amount 1 --concurrency 5 --timeout 60 \
  | tee /tmp/concurrent-transactions.json
```

**Expected observations and database checks:** A healthy run reports `PASS`. If another
failure is intentionally combined with it, reconcile every UUID instead of assuming
all succeeded. The two-account sum remains constant, each UUID has one Coordinator
row, COMMIT decisions resolve, and no resolved transaction leaves a prepared GID:

```sh
refresh_primaries
kubectl exec "$COORD_PRIMARY" -- psql -U postgres -d banking_coordinator -c \
  "SELECT state, decision, count(*) FROM coordinated_transfers
     GROUP BY state, decision ORDER BY state, decision;"
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT id, balance FROM accounts WHERE id=$ACCOUNT_A;" \
  -c "SELECT gid FROM pg_prepared_xacts;"
kubectl exec "$B_PRIMARY" -- psql -U postgres -d banking_b -c \
  "SELECT id, balance FROM accounts WHERE id=$ACCOUNT_B;" \
  -c "SELECT gid FROM pg_prepared_xacts;"
```

**Cleanup:** Keep JSON and SQL output. Use new UUIDs for another payload.

## 4. Coordinator Interruption

This uses the existing durable-COMMIT crash hook.

**Prerequisites and baseline:** Record both balances and generate one transaction UUID:

```sh
TX_ID="$("$PYTHON" -c 'import uuid; print(uuid.uuid4())')"
kubectl set env deployment/coordinator \
  COORDINATOR_EXPERIMENT_CRASH_AFTER_COMMIT_TRANSACTION_ID="$TX_ID"
kubectl rollout status deployment/coordinator
COORDINATOR_POD="$(kubectl get pods -l app=coordinator \
  -o jsonpath='{.items[0].metadata.name}')"
COORDINATOR_UID="$(kubectl get pod "$COORDINATOR_POD" \
  -o jsonpath='{.metadata.uid}')"
RESTARTS_BEFORE="$(kubectl get pod "$COORDINATOR_POD" \
  -o jsonpath='{.status.containerStatuses[0].restartCount}')"
test "$(kubectl exec "$COORDINATOR_POD" -- printenv \
  COORDINATOR_EXPERIMENT_CRASH_AFTER_COMMIT_TRANSACTION_ID)" = "$TX_ID"
echo "$TX_ID"
```

**Failure injection:** Submit exactly one transfer with that UUID. A connection failure
is ambiguous; do not create another UUID.

```sh
"$PYTHON" -m client --json transfer \
  --source-account-id "$ACCOUNT_A" --destination-account-id "$ACCOUNT_B" \
  --amount 1 --request-id "$TX_ID" || true
RESTARTS_AFTER="$RESTARTS_BEFORE"
for attempt in $(seq 1 60); do
  CURRENT_COORDINATOR_POD="$(kubectl get pods -l app=coordinator \
    -o jsonpath='{.items[0].metadata.name}')"
  CURRENT_COORDINATOR_UID="$(kubectl get pod "$CURRENT_COORDINATOR_POD" \
    -o jsonpath='{.metadata.uid}')"
  if test "$CURRENT_COORDINATOR_UID" != "$COORDINATOR_UID"; then
    echo "INVALID RESULT: Coordinator Pod was replaced rather than restarted" >&2
    break
  fi
  RESTARTS_AFTER="$(kubectl get pod "$CURRENT_COORDINATOR_POD" \
    -o jsonpath='{.status.containerStatuses[0].restartCount}')"
  test "$RESTARTS_AFTER" -gt "$RESTARTS_BEFORE" && break
  sleep 2
done
test "$CURRENT_COORDINATOR_POD" = "$COORDINATOR_POD"
test "$CURRENT_COORDINATOR_UID" = "$COORDINATOR_UID"
test "$RESTARTS_AFTER" -gt "$RESTARTS_BEFORE"
kubectl wait --for=condition=Ready pod/"$CURRENT_COORDINATOR_POD" --timeout=120s
kubectl logs "$CURRENT_COORDINATOR_POD" --previous | \
  grep 'Experiment fault: exiting after durable COMMIT'
kubectl get pods -l app=coordinator
```

**Expected observations and database checks:** The Pod restarts, recovery reuses
`$TX_ID`, the durable decision remains COMMIT, participants become COMMITTED, balances
move once, and prepared GIDs disappear:

```sh
refresh_primaries
COORD_RESULT="$(kubectl exec "$COORD_PRIMARY" -- \
  psql -U postgres -d banking_coordinator -Atc \
  "SELECT state || '|' || decision || '|' || source_state || '|' || destination_state
     FROM coordinated_transfers WHERE transaction_id='$TX_ID';")"
test "$COORD_RESULT" = "RESOLVED|COMMIT|COMMITTED|COMMITTED"
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT transaction_id, state, gid FROM participant_transactions
     WHERE transaction_id='$TX_ID';" -c "SELECT gid FROM pg_prepared_xacts;"
kubectl exec "$B_PRIMARY" -- psql -U postgres -d banking_b -c \
  "SELECT transaction_id, state, gid FROM participant_transactions
     WHERE transaction_id='$TX_ID';" -c "SELECT gid FROM pg_prepared_xacts;"
test "$(kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -Atc \
  "SELECT state FROM participant_transactions WHERE transaction_id='$TX_ID';")" \
  = COMMITTED
test "$(kubectl exec "$B_PRIMARY" -- psql -U postgres -d banking_b -Atc \
  "SELECT state FROM participant_transactions WHERE transaction_id='$TX_ID';")" \
  = COMMITTED
test "$(kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -Atc \
  "SELECT count(*) FROM pg_prepared_xacts WHERE gid='mbts:A:$TX_ID';")" -eq 0
test "$(kubectl exec "$B_PRIMARY" -- psql -U postgres -d banking_b -Atc \
  "SELECT count(*) FROM pg_prepared_xacts WHERE gid='mbts:B:$TX_ID';")" -eq 0
"$PYTHON" -m client request get "$TX_ID"
```

**Cleanup:** Remove the hook and wait for a healthy Coordinator. Never resolve
participants manually.

```sh
kubectl set env deployment/coordinator \
  COORDINATOR_EXPERIMENT_CRASH_AFTER_COMMIT_TRANSACTION_ID-
kubectl rollout status deployment/coordinator
```

## 5. Autoscaling Under Load

**Prerequisites and baseline:** Metrics Server must work. Record CPU, HPA TARGETS,
desired/current replicas, and balance before load:

```sh
BASE_REPLICAS="$(kubectl get deployment bank-a \
  -o jsonpath='{.status.replicas}')"
{
  date
  kubectl top pods -l app=bank-a
  kubectl get hpa bank-a bank-b
  kubectl get deployment bank-a bank-b
} | tee /tmp/hpa-before.txt
"$PYTHON" -m client balance --account-id "$ACCOUNT_A"
refresh_primaries
REQUEST_COUNT_BEFORE="$(kubectl exec "$A_PRIMARY" -- \
  psql -U postgres -d banking_a -Atc \
  "SELECT count(*) FROM requests WHERE requested_account_id=$ACCOUNT_A;")"
```

**Load injection:** Reuse the bounded generator repeatedly. Balance-only load avoids
changing money while exercising scaling:

```sh
HPA_SCALED_EVIDENCE="/tmp/hpa-scaled.$$"
test ! -e "$HPA_SCALED_EVIDENCE"
(
  for run in $(seq 1 20); do
    "$PYTHON" -m load_generator --scenario balance \
      --account-id "$ACCOUNT_A" --requests 1000 --concurrency 50 --json || true
  done
) > /tmp/hpa-load.jsonl &
LOAD_PID=$!
(
  for sample in $(seq 1 30); do
    date
    kubectl top pods -l app=bank-a
    kubectl get hpa bank-a
    CURRENT_REPLICAS="$(kubectl get deployment bank-a \
      -o jsonpath='{.status.replicas}')"
    echo "baseline_replicas=$BASE_REPLICAS current_replicas=$CURRENT_REPLICAS"
    test "$CURRENT_REPLICAS" -gt "$BASE_REPLICAS" && \
      touch "$HPA_SCALED_EVIDENCE"
    sleep 5
  done
) | tee /tmp/hpa-during.txt &
MONITOR_PID=$!
```

After the bounded load and sampling finish, record the final CPU, TARGETS, and replica
counts. This experiment passes only if a sample observed more replicas than baseline:

```sh
wait "$LOAD_PID" || true
wait "$MONITOR_PID"
{
  date
  kubectl top pods -l app=bank-a
  kubectl get hpa bank-a bank-b
  kubectl get deployment bank-a bank-b
} | tee /tmp/hpa-after.txt
test -f "$HPA_SCALED_EVIDENCE"
kubectl get pods -l app=bank-a -o wide
kubectl get endpointslice -l kubernetes.io/service-name=bank-a
```

**Expected observations and database checks:** Average CPU crosses the 70% target and
HPA actually scales from 2 toward 4. All Ready scaled Pods appear behind `bank-a`.
Balance-only load must not change the account or create mutation rows:

```sh
"$PYTHON" -m client balance --account-id "$ACCOUNT_A"
refresh_primaries
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT id, balance FROM accounts WHERE id=$ACCOUNT_A;" \
  -c "SELECT count(*) FROM requests WHERE requested_account_id=$ACCOUNT_A;"
REQUEST_COUNT_AFTER="$(kubectl exec "$A_PRIMARY" -- \
  psql -U postgres -d banking_a -Atc \
  "SELECT count(*) FROM requests WHERE requested_account_id=$ACCOUNT_A;")"
test "$REQUEST_COUNT_AFTER" -eq "$REQUEST_COUNT_BEFORE"
```

**Cleanup:** Let HPA scale down naturally; do not manually set replicas while HPA owns
the scale target.

## 6. Rolling Update Under Load

**Prerequisites and baseline:** Two Banking images must already be present in Minikube.
Do not invent or download a tag. Export the exact second image reference beforehand,
then verify Minikube already has it before changing the Deployment:

```sh
OLD_IMAGE="$(kubectl get deployment bank-a \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="bank")].image}')"
: "${NEW_IMAGE:?export NEW_IMAGE as a real second image already in Minikube}"
test -n "$OLD_IMAGE" && test "$NEW_IMAGE" != "$OLD_IMAGE"
minikube image ls | grep -F -- "$NEW_IMAGE"
"$PYTHON" -m client balance --account-id "$ACCOUNT_A"
```

**Load and update injection:** Run mixed load and the configured zero-unavailable,
one-surge rollout:

```sh
"$PYTHON" -m load_generator --scenario mixed \
  --account-id "$ACCOUNT_A" --amount 1 --requests 1000 --concurrency 30 --json \
  > /tmp/rolling-update-load.json &
LOAD_PID=$!
kubectl set image deployment/bank-a bank="$NEW_IMAGE"
kubectl rollout status deployment/bank-a --timeout=180s
kubectl get pods -l app=bank-a -o wide
kubectl get endpointslice -l kubernetes.io/service-name=bank-a
wait "$LOAD_PID" || true
```

**Rollback:** Restore and verify the prior revision:

```sh
kubectl rollout undo deployment/bank-a
kubectl rollout status deployment/bank-a --timeout=180s
RESTORED_IMAGE="$(kubectl get deployment bank-a \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="bank")].image}')"
test "$RESTORED_IMAGE" = "$OLD_IMAGE"
echo "$RESTORED_IMAGE"
kubectl rollout history deployment/bank-a
```

**Expected observations and database checks:** Ready capacity never drops below the
minimum. Reconcile UNKNOWN results by their original UUIDs. Final balance equals the
initial balance plus durable successful deposits minus durable successful withdrawals,
and every UUID has at most one row:

```sh
refresh_primaries
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT request_id, count(*) FROM requests
     WHERE requested_account_id=$ACCOUNT_A GROUP BY request_id HAVING count(*) > 1;" \
  -c "SELECT id, balance FROM accounts WHERE id=$ACCOUNT_A;"
"$PYTHON" -m client pending recover
```

**Cleanup:** Confirm the restored image equals `$OLD_IMAGE`, all Pods are Ready, and
retain load JSON plus rollout history.

## 7. Repeated Client Requests

**Prerequisites and baseline:** Record one dedicated account balance and generate one
UUID:

```sh
refresh_primaries
REQUEST_ID="$("$PYTHON" -c 'import uuid; print(uuid.uuid4())')"
BEFORE="$(kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -Atc \
  "SELECT balance FROM accounts WHERE id=$ACCOUNT_A;")"
echo "request=$REQUEST_ID before=$BEFORE"
```

**Repeated request injection:** Submit an identical payload twice with the same key:

```sh
"$PYTHON" -m client --json deposit \
  --account-id "$ACCOUNT_A" --amount 7 --request-id "$REQUEST_ID"
"$PYTHON" -m client --json deposit \
  --account-id "$ACCOUNT_A" --amount 7 --request-id "$REQUEST_ID"
"$PYTHON" -m client request get "$REQUEST_ID"
```

**Expected observations and database checks:** Both responses identify the same request,
the replay returns its durable result, balance increases exactly once by 7, and exactly
one request row exists:

```sh
refresh_primaries
kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -c \
  "SELECT request_id, operation, status, amount, balance_before, balance_after
     FROM requests WHERE request_id='$REQUEST_ID';" \
  -c "SELECT count(*) AS rows_for_id FROM requests
        WHERE request_id='$REQUEST_ID';" \
  -c "SELECT id, balance FROM accounts WHERE id=$ACCOUNT_A;"
AFTER="$(kubectl exec "$A_PRIMARY" -- psql -U postgres -d banking_a -Atc \
  "SELECT balance FROM accounts WHERE id=$ACCOUNT_A;")"
test "$AFTER" -eq $((BEFORE + 7))
```

**Cleanup:** Keep the request row as evidence. Never reuse `$REQUEST_ID` with another
account, amount, or operation.

## Observed results

These are the results observed during the manual execution of the seven experiments.
They document that run, not guarantees beyond the tested conditions.

1. **Application Pod Failure:** The deleted Banking Pod was automatically replaced
   and the Deployment returned to Ready. All 6,000 of 6,000 load requests succeeded;
   database balance consistency was verified (`actual=10000`, `expected=10000`).
2. **Primary Database Failure:** PostgreSQL recovered after Primary Pod deletion and
   committed data survived. A controlled Patroni switchover changed the Primary from
   `postgres-a-0` to `postgres-a-1`, advanced the timeline to 3, updated the Primary
   Service endpoint, and restored synchronous replication. Application reads and
   writes succeeded afterward. The failure injection did not independently
   demonstrate automatic Standby promotion.
3. **Concurrent Transactions:** All 20 cross-shard transfers succeeded. The
   Coordinator recorded 20 `RESOLVED`/`COMMIT` transactions, both participants
   recorded 20 `COMMITTED` transactions, no prepared transactions remained, and
   money conservation was verified.
4. **Coordinator Interruption:** The Coordinator crashed after recording durable
   `COMMIT`; its container restart count increased from 0 to 1. Recovery preserved
   the original transaction ID and reached `RESOLVED`/`COMMIT`, both participants
   reached `COMMITTED`, no unresolved prepared transactions remained, and money
   conservation was verified.
5. **Autoscaling Under Load:** Metrics Server supplied CPU metrics and the HPA scaled
   Bank A from 2 to 4 replicas. All 20,000 of 20,000 read requests succeeded and the
   database balance remained unchanged.
6. **Rolling Update Under Load:** A second image differing only by a nonfunctional
   Docker label was rolled out and rolled back successfully, restoring the original
   image. Of 4,000 requests, 3,997 succeeded and three `WITHDRAW` requests returned
   `UNKNOWN`; no durable request records existed for those three IDs. The balance
   changed from 9981 to 9984, consistent with the observed outcomes. The `UNKNOWN`
   requests are neither counted as successful nor classified as definitively aborted.
   Because the images were functionally identical, application-version compatibility
   was not tested.
7. **Repeated Client Requests:** Two identical deposits reused one Request ID. The
   first response reported `replayed=false`, the second `replayed=true`, the balance
   changed once from 5000 to 5100, and exactly one durable request record existed.
