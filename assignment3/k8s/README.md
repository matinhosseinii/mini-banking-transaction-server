# Kubernetes application manifests

This directory contains the Kubernetes application and PostgreSQL resources for
Assignment 3:

- `configmap.yaml`: non-sensitive runtime settings and internal Service URLs.
- `secret.example.yaml`: development-only template for database connection URLs.
- `applications.yaml`: Gateway, Coordinator, and two bank shard Deployments and Services.
- `hpa.yaml`: CPU-based autoscaling for the two Banking Deployments.
- `networkpolicy.yaml`: Banking ingress allowed only from Gateway and Coordinator Pods.
- `postgres/rbac.yaml`: namespace-scoped Patroni ServiceAccount and RBAC.
- `postgres/config.yaml`: shared Patroni bootstrap configuration and database init hook.
- `postgres/Dockerfile`: minimal PostgreSQL 17 + Patroni Kubernetes image.
- `postgres/clusters.yaml`: three independent two-member PostgreSQL StatefulSets,
  headless Services, primary-only Services, and per-Pod volume claim templates.
- `overlays/dockerhub/`: image-only overlay for the published Assignment 3 images.

Each `postgres-a`, `postgres-b`, and `postgres-coordinator` StatefulSet runs one
Patroni-elected Primary and one synchronous Standby. Patroni stores leader state in
Kubernetes ConfigMaps and labels the current leader `role=primary`; the corresponding
`*-primary` Service selects only that label. No Pod ordinal is assumed to be Primary.

## Images and configuration

The original manifests use local Assignment 3 image names:

- `banking-gateway:assignment3`
- `banking-coordinator:assignment3`
- `banking-server:assignment3`
- `banking-postgres-patroni:assignment3`

The Docker Hub overlay replaces those names with the published images without changing
the original manifests:

- `matinhosseinii/banking-gateway:assignment3`
- `matinhosseinii/banking-coordinator:assignment3`
- `matinhosseinii/banking-server:assignment3`
- `matinhosseinii/banking-postgres-patroni:assignment3`

The database StatefulSets use the local image
`banking-postgres-patroni:assignment3`. Its Dockerfile extends
`postgres:17`, installs the pinned Patroni Kubernetes extra, and starts
`patroni /etc/patroni/patroni.yml` as the image's existing `postgres` user. The base
image supplies PostgreSQL utilities including `psql` and `pg_rewind`. The PVC is
mounted at the base image's `/var/lib/postgresql/data` root, Patroni stores the cluster
under its `pgdata` child directory, and Pod `fsGroup: 999` gives the Debian PostgreSQL
user access to a newly mounted volume.

Build the image manually from the repository root:

```sh
docker build -t banking-postgres-patroni:assignment3 \
  assignment3/k8s/postgres
```

The first uncached build may download `postgres:17`, Debian packages for Python, pip
dependencies for `patroni[kubernetes]`, and their transitive Kubernetes client
dependencies. The build therefore requires registry and package-index access; this
commit did not execute it.

Make the completed local image available to Minikube before applying the StatefulSets:

```sh
minikube image load banking-postgres-patroni:assignment3
```

Alternatively, point Docker at Minikube before running the same build command:

```sh
eval "$(minikube docker-env)"
docker build -t banking-postgres-patroni:assignment3 \
  assignment3/k8s/postgres
```

Copy `secret.example.yaml` to a local file outside version control and replace every
development-only credential. The database URLs already use the future Patroni primary
Service names `postgres-a-primary`, `postgres-b-primary`, and
`postgres-coordinator-primary`. Keep each URL password synchronized with
`BANK_A_PASSWORD`, `BANK_B_PASSWORD`, or `COORDINATOR_PASSWORD`; the init hook creates
the matching database and login once, during initial Patroni bootstrap. The shared
superuser and replication examples are also development-only.

## PostgreSQL behavior

Patroni bootstraps PostgreSQL with streaming replication, replication slots, data
checksums, `max_prepared_transactions=20`, and strict synchronous mode with one
synchronous Standby. `maximum_lag_on_failover=0` and timeline checking reject a known
lagging or divergent promotion candidate. Strict synchronous mode intentionally stops
acknowledging writes when no synchronous Standby is available; this favors durability
over availability.

Each Pod gets its own `ReadWriteOnce` 1 Gi PVC from the StatefulSet. PVCs survive Pod
replacement and are not deleted by removing a StatefulSet. StorageClass selection is
left to the cluster default. Patroni's `/readiness` endpoint verifies that the local
member is running as a usable PostgreSQL Primary or replica, while primary routing is
enforced separately by the Patroni-managed `role=primary` Service selector.

## Manual deployment (for later use)

Create a local Secret from the example, build or load the custom image into Minikube,
and then apply resources in this order:

```sh
kubectl apply -f assignment3/k8s/configmap.yaml
kubectl apply -f /path/to/local/banking-secret.yaml
kubectl apply -f assignment3/k8s/postgres/rbac.yaml
kubectl apply -f assignment3/k8s/postgres/config.yaml
kubectl apply -f assignment3/k8s/postgres/clusters.yaml
# Wait for all three Patroni clusters to bootstrap before starting applications.
kubectl apply -f assignment3/k8s/applications.yaml
kubectl get deployments,pods,services
```

The Gateway is exposed on NodePort `30080`; all other Services are ClusterIP-only.

### Docker Hub deployment overlay

The default file-by-file commands above use images built or loaded locally. The Docker
Hub overlay reuses the same ConfigMaps, RBAC, Services, workloads, PVC templates, HPA,
rolling-update settings, and NetworkPolicy, changing only the four image references.
It intentionally does not include `secret.example.yaml`. Create `banking-secrets`
from a private local file using the existing Secret workflow before deployment.

Inspect the complete rendered resources without applying them:

```sh
kubectl kustomize --load-restrictor LoadRestrictionsNone \
  assignment3/k8s/overlays/dockerhub
```

The load-restrictor option permits this nested overlay to reuse its parent manifests;
it does not enable network access. After creating the Secret, deploy the rendered
overlay when registry access is available:

```sh
kubectl kustomize --load-restrictor LoadRestrictionsNone \
  assignment3/k8s/overlays/dockerhub | kubectl apply -f -
```

Verify the images configured on all seven workloads:

```sh
kubectl get deployment gateway coordinator bank-a bank-b \
  -o custom-columns='KIND:.kind,NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image'
kubectl get statefulset postgres-a postgres-b postgres-coordinator \
  -o custom-columns='KIND:.kind,NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image'
```

Reproducible deployment requires the referenced tags to remain available, cluster
access to Docker Hub (including any required registry credentials or proxy settings),
a default `ReadWriteOnce` StorageClass, Metrics Server for HPA metrics, a
NetworkPolicy-capable CNI for policy enforcement, and database migration and seed
steps described below. For immutable production-style reproduction, pin published
images by digest in a reviewed overlay rather than relying on mutable tags.

## Banking autoscaling

The cluster must provide the Kubernetes resource metrics API, normally through Metrics
Server. These manifests do not install or enable it. After Metrics Server and the
Banking Deployments are available, apply and inspect the HPAs:

```sh
kubectl apply -f assignment3/k8s/hpa.yaml
kubectl get hpa bank-a bank-b
kubectl get hpa bank-a bank-b --watch
```

Observe replica changes and confirm that each shard Service includes newly Ready Pods:

```sh
kubectl get deployment bank-a bank-b --watch
kubectl get pods -l app=bank-a --watch
kubectl get pods -l app=bank-b --watch
kubectl get endpointslice -l kubernetes.io/service-name=bank-a --watch
kubectl get endpointslice -l kubernetes.io/service-name=bank-b --watch
```

Each HPA keeps at least two Banking Pods, permits up to four, and targets average CPU
utilization of 70% relative to the existing `50m` container CPU request. Gateway and
Coordinator continue using the stable `bank-a` and `bank-b` Service names, so no
routing configuration changes when Pods are added or removed.

## Banking rolling updates and rollback

The Banking Deployments use `maxUnavailable: 0` and `maxSurge: 1`. A replacement Pod
must pass `/ready` before it receives Service traffic, and terminating Pods are removed
from Service endpoints. Uvicorn handles SIGTERM and in-flight requests during the
30-second Pod termination grace period; no custom drain or mutation retry is added.

Runtime testing requires two Banking Server image versions that are already available
to the cluster. Set the complete new image reference using the existing repository
naming convention, then update both shards:

```sh
NEW_BANKING_IMAGE='matinhosseinii/banking-server:<available-new-tag>'
kubectl set image deployment/bank-a bank="$NEW_BANKING_IMAGE"
kubectl set image deployment/bank-b bank="$NEW_BANKING_IMAGE"
kubectl rollout status deployment/bank-a
kubectl rollout status deployment/bank-b
```

Watch readiness, rollout progress, and Service endpoint membership:

```sh
kubectl get deployment bank-a bank-b --watch
kubectl get pods -l app=bank-a
kubectl get pods -l app=bank-b
kubectl get endpointslice -l kubernetes.io/service-name=bank-a
kubectl get endpointslice -l kubernetes.io/service-name=bank-b
```

Rollback and verify the restored revision:

```sh
kubectl rollout undo deployment/bank-a
kubectl rollout undo deployment/bank-b
kubectl rollout status deployment/bank-a
kubectl rollout status deployment/bank-b
kubectl rollout history deployment/bank-a
kubectl rollout history deployment/bank-b
```

Run normal balance, mutation, and cross-shard transfer commands during the rollout.
Retain each reported request ID, then verify its result instead of issuing an unrelated
replacement mutation after an ambiguous response:

```sh
REQUEST_ID='replace-with-request-uuid'
ACCOUNT_ID='replace-with-account-id'
(cd assignment3 && ../.venv/bin/python -m client pending recover)
(cd assignment3 && ../.venv/bin/python -m client request get "$REQUEST_ID")
(cd assignment3 && ../.venv/bin/python -m client balance --account-id "$ACCOUNT_ID")
```

HPA may change the desired replica count during a rollout, but the Deployment strategy
still permits no unavailable Banking Pods and only one surge Pod per shard.

## Banking ingress policy

Apply the policy after the application resources:

```sh
kubectl apply -f assignment3/k8s/networkpolicy.yaml
kubectl describe networkpolicy banking-ingress
```

The policy selects both Banking Deployments and permits TCP port 8000 only from Pods
labelled `app=gateway` or `app=coordinator` in the same namespace. It defines no egress
policy, so DNS and PostgreSQL access remain unrestricted. It does not restrict Patroni
or PostgreSQL Pods.

A NetworkPolicy object has no effect unless the cluster CNI enforces NetworkPolicy.
Kindnet must not be assumed to enforce it merely because Kubernetes accepts the YAML.
Verify behavior manually with the already deployed application images:

```sh
# Both permitted calls must return the Banking readiness response.
kubectl exec deployment/gateway -- python -c \
  "import urllib.request; print(urllib.request.urlopen('http://bank-a:8000/ready').status)"
kubectl exec deployment/coordinator -- python -c \
  "import urllib.request; print(urllib.request.urlopen('http://bank-b:8000/ready').status)"

# This unrelated Pod must time out or be refused when policy enforcement is active.
kubectl run networkpolicy-test --restart=Never \
  --image=matinhosseinii/banking-server:assignment2 \
  --labels=app=networkpolicy-test --command -- sleep 3600
kubectl wait --for=condition=Ready pod/networkpolicy-test --timeout=60s
kubectl exec networkpolicy-test -- python -c \
  "import urllib.request; urllib.request.urlopen('http://bank-a:8000/ready', timeout=3)"
kubectl delete pod networkpolicy-test
```

If the unrelated request succeeds, the CNI is not enforcing this policy; select and
configure a NetworkPolicy-capable CNI manually before claiming isolation. The manifests
do not install or change the CNI.

## Minimal observability

Use existing application logs and Kubernetes status:

```sh
kubectl logs deployment/gateway --tail=100
kubectl logs -l app=bank-a --all-containers=true --prefix --tail=100
kubectl logs -l app=bank-b --all-containers=true --prefix --tail=100
kubectl logs deployment/coordinator --tail=200
kubectl logs deployment/coordinator --since=30m | grep 'recovery'
kubectl get pods -o wide
kubectl get pods -L app,shard,role
POD_NAME='replace-with-pod-name'
kubectl describe pod "$POD_NAME"
```

Inspect Patroni membership, replication, and prepared transactions:

```sh
kubectl exec postgres-a-0 -- patronictl -c /etc/patroni/patroni.yml list
PRIMARY_POD="$(kubectl get pods \
  -l 'application=patroni,cluster-name=postgres-a,role=primary' \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl exec "$PRIMARY_POD" -- psql -U postgres -d postgres -c \
  "SELECT application_name, state, sync_state FROM pg_stat_replication;"
kubectl exec "$PRIMARY_POD" -- psql -U postgres -d banking_a -c \
  "SELECT gid, prepared, owner, database FROM pg_prepared_xacts;"
```

Repeat the database commands for the other cluster labels and database names. Resource
metrics require Metrics Server:

```sh
kubectl top pods
kubectl top pods -l app=bank-a
kubectl top pods -l app=bank-b
kubectl get hpa bank-a bank-b
kubectl describe hpa bank-a
kubectl describe hpa bank-b
```

Check a transaction outcome through the existing client/API using its original ID:

```sh
REQUEST_ID='replace-with-request-uuid'
(cd assignment3 && ../.venv/bin/python -m client request get "$REQUEST_ID")
```

Inspect roles and streaming state after deployment:

```sh
kubectl get pods -L cluster-name,role
kubectl get configmaps -l application=patroni -L cluster-name
kubectl get endpoints postgres-a-primary postgres-b-primary postgres-coordinator-primary

# Discover the elected postgres-a Primary; no StatefulSet ordinal is privileged.
PRIMARY_POD="$(kubectl get pods \
  -l 'application=patroni,cluster-name=postgres-a,role=primary' \
  -o jsonpath='{.items[0].metadata.name}')"
test -n "$PRIMARY_POD"

kubectl exec "$PRIMARY_POD" -- patronictl -c /etc/patroni/patroni.yml list
kubectl exec "$PRIMARY_POD" -- psql -U postgres -d postgres -c \
  "SELECT application_name, client_addr, state, sync_state FROM pg_stat_replication;"
kubectl exec "$PRIMARY_POD" -- psql -U postgres -d postgres -c \
  "SHOW synchronous_standby_names;"
kubectl exec "$PRIMARY_POD" -- psql -U postgres -d banking_a -c \
  "SELECT gid, prepared, owner, database FROM pg_prepared_xacts;"
```

For a healthy two-member cluster, `pg_stat_replication` should show one row with
`state=streaming` and `sync_state=sync`, and `synchronous_standby_names` should name
the synchronous candidate. Run the equivalent commands with the `postgres-b` and
`postgres-coordinator` cluster labels and databases. The chosen image may require
supplying the superuser password to `psql`; follow that image's documented local
authentication behavior.

## Current dependencies and limitations

- The custom image build and Patroni dependency resolution were not executed. Confirm
  the pinned Patroni release remains available from the configured package index and
  that Debian package versions resolve when performing the first networked build.
- Patroni uses ConfigMaps for its Kubernetes DCS because Endpoint-based TTL-lock
  acquisition repeatedly failed in the observed Kubernetes 1.37 environment. RBAC
  retains Endpoint permissions, but leader election no longer depends on Endpoints.
  The exact API-server rejection was not available offline, so this change addresses
  the failing DCS path without claiming a proven Kubernetes root cause.
- The Gateway and Coordinator use one logical URL per shard: `http://bank-a:8000` and
  `http://bank-b:8000`. The ClusterIP Services distribute new connections across
  Ready Banking Pods; HTTP connection reuse means requests are not guaranteed to be
  evenly distributed. The applications do not model repeated calls to one Service as
  independent replica failover.
- Gateway forwarding remains single-attempt, including for mutations. A transport
  failure is returned to the client rather than blindly replayed against another Pod.
  Coordinator 2PC actions also make one Service request per recovery attempt; later
  durable recovery attempts reuse the same transaction ID.
- Banking and Coordinator SQLAlchemy engines validate pooled connections before
  checkout. This discards connections made stale by a Primary change but does not
  retry an in-flight or ambiguously committed transaction. Coordinator recovery logs
  a failed database cycle and continues with the original transaction IDs on later
  cycles after the Primary Service becomes available again.
- Only readiness probes are configured. They retain the existing `/ready` database
  and downstream-health semantics. Liveness probes are intentionally omitted so a
  temporary database or routing outage cannot trigger restart loops.
- Apply the Coordinator and bank Deployments only after their databases exist. The
  Coordinator image applies its own Alembic migration at startup, while shard schema
  migration and seeding remain explicit steps in the existing workflow and must be
  run against the new Primary Services before serving traffic. This commit does not
  invent a second migration system or automate that operational step.
- PostgreSQL replicates prepared transactions in WAL, but replication alone does not
  decide whether an in-doubt entry in `pg_prepared_xacts` must commit or roll back.
  Correct resolution still depends on the durable Coordinator decision and existing
  recovery behavior, neither of which is changed or failure-tested here.
- Patroni's Kubernetes leader lock, timeline checks, zero failover-lag threshold, and
  strict synchronous writes reduce stale promotion and split-brain risk, but fencing
  depends on working Kubernetes API/DCS semantics and the selected image. They are not
  proof against every partition or storage failure. A Primary failure can interrupt or
  leave an uncertain transaction outcome while election and application retry/recovery
  occur. Automated failover, prepared-transaction recovery, and fencing have not been
  deployed or verified in this commit.
