# Kubernetes application manifests

This directory contains the Kubernetes application and PostgreSQL resources for
Assignment 3:

- `configmap.yaml`: non-sensitive runtime settings and internal Service URLs.
- `secret.example.yaml`: development-only template for database connection URLs.
- `applications.yaml`: Gateway, Coordinator, and two bank shard Deployments and Services.
- `postgres/rbac.yaml`: namespace-scoped Patroni ServiceAccount and RBAC.
- `postgres/config.yaml`: shared Patroni bootstrap configuration and database init hook.
- `postgres/Dockerfile`: minimal PostgreSQL 17 + Patroni Kubernetes image.
- `postgres/clusters.yaml`: three independent two-member PostgreSQL StatefulSets,
  headless Services, primary-only Services, and per-Pod volume claim templates.

Each `postgres-a`, `postgres-b`, and `postgres-coordinator` StatefulSet runs one
Patroni-elected Primary and one synchronous Standby. Patroni stores leader state in
Kubernetes ConfigMaps and labels the current leader `role=primary`; the corresponding
`*-primary` Service selects only that label. No Pod ordinal is assumed to be Primary.

## Images and configuration

The manifests reuse the Assignment 2 images already named by
`infra/standalone/compose.hub.yaml`:

- `matinhosseinii/banking-gateway:assignment2`
- `matinhosseinii/banking-coordinator:assignment2`
- `matinhosseinii/banking-server:assignment2`

Build and publish replacement images separately if these images are unavailable, then
edit the three `image` fields. This commit does not build or push images.

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
