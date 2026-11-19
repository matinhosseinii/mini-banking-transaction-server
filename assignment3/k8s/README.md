# Kubernetes application manifests

This directory contains the application-only Kubernetes resources for Assignment 3:

- `configmap.yaml`: non-sensitive runtime settings and internal Service URLs.
- `secret.example.yaml`: development-only template for database connection URLs.
- `applications.yaml`: Gateway, Coordinator, and two bank shard Deployments and Services.

No PostgreSQL or Patroni resources are included yet.

## Images and configuration

The manifests reuse the Assignment 2 images already named by
`infra/standalone/compose.hub.yaml`:

- `matinhosseinii/banking-gateway:assignment2`
- `matinhosseinii/banking-coordinator:assignment2`
- `matinhosseinii/banking-server:assignment2`

Build and publish replacement images separately if these images are unavailable, then
edit the three `image` fields. This commit does not build or push images.

Copy `secret.example.yaml` to a local file outside version control and replace every
development-only credential. The database URLs already use the future Patroni primary
Service names `postgres-a-primary`, `postgres-b-primary`, and
`postgres-coordinator-primary`.

## Manual deployment (for later use)

After the database Services and replica-routing compatibility work exist:

```sh
kubectl apply -f assignment3/k8s/configmap.yaml
kubectl apply -f /path/to/local/banking-secret.yaml
kubectl apply -f assignment3/k8s/applications.yaml
kubectl get deployments,pods,services
```

The Gateway is exposed on NodePort `30080`; all other Services are ClusterIP-only.

## Current dependencies and limitations

- The three expected PostgreSQL/Patroni primary Services are not deployed in this
  commit. Bank and Coordinator readiness therefore remains false until they exist,
  and the Coordinator image's startup migration cannot complete without its database.
- The Gateway and Coordinator still require the fixed `BANK_A1_URL`, `BANK_A2_URL`,
  `BANK_B1_URL`, and `BANK_B2_URL` configuration inherited from Assignment 2. A
  Deployment plus one shard Service cannot give those names independent pod identity
  or health-aware replica routing. The ConfigMap therefore reserves the future DNS
  names `bank-a1`, `bank-a2`, `bank-b1`, and `bank-b2`, but this commit deliberately
  does not create misleading Services for them. Requests and Gateway readiness will
  not work until the planned application compatibility/discovery change is complete.
- The `bank-a` and `bank-b` ClusterIP Services provide ordinary load-balanced access
  to each shard but are not used as substitutes for the four fixed replica endpoints.
- Only readiness probes are configured. They retain the existing `/ready` database
  and downstream-health semantics. Liveness probes are intentionally omitted so a
  temporary database or routing outage cannot trigger restart loops.
- Database migrations and initial data still depend on the existing image startup and
  later database provisioning workflow. No availability claim is made by these
  application-only resources.
