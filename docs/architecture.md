# System Architecture

Mini Banking Transaction Server is a simulated banking system for a university Distributed Systems project. It uses synthetic accounts and transactions, and is planned to provide concurrent balance inquiry, deposit, and withdrawal with ACID persistence, logging, and error handling. Transfer is not supported.

Development starts with a Standalone deployment and progresses to high availability, automatic failover, and network-partition handling. No described functionality has been implemented or verified yet.

## System Overview

The project uses a modular monorepo and targets Python 3.12. An interactive CLI and load generator share an HTTP client for communicating with the Banking Server. The server manages persistent shared state in PostgreSQL.

Docker Compose is used for deployment. Kubernetes and Docker Swarm are not part of the architecture.

## Deployment Architecture

The Standalone deployment consists of one FastAPI Banking Server, one PostgreSQL instance, and a separate Client VM communicating over HTTP. It is the deployment mode to implement and verify before HA work.

The HA deployment uses four VMs, each with an independent Docker Compose deployment:

```text
Client VM ── HTTP ──> Witness: HAProxy ──> Server A: FastAPI + PostgreSQL
                                             Server B: FastAPI + PostgreSQL

Server A, Server B, and Witness:
  - Patroni-managed PostgreSQL
  - One etcd member each
```

Server A and Server B run identical Banking Server code and image versions. PostgreSQL runs on Server A, Server B, and Witness; Patroni manages each database instance, and the three etcd members form the coordination cluster.

HAProxy runs on Witness and routes clients to the valid ready Primary. Witness runs no Banking Server: its PostgreSQL instance is replica-only, uses `nofailover`, and cannot be promoted. Only Server A and Server B are Primary candidates.

The planned VM network uses NAT for internet access and Host-Only networking with static IP addresses for inter-VM communication. Containers use Docker Bridge networking.

## Banking Application and Database

FastAPI uses synchronous endpoints. SQLAlchemy ORM 2.x provides synchronous sessions and connection pooling, while Alembic manages schema migrations. The database schema contains `accounts`, `requests`, and `transactions`, including a transaction ledger.

Monetary values use PostgreSQL `BIGINT`. Transaction amounts must be positive integers, while account balances must be non-negative. Floating-point monetary storage is not used.

READ COMMITTED isolation, atomic conditional SQL updates, and row-level locking prevent lost updates. A balance modification, idempotency record, and ledger insertion must be committed atomically.

These mechanisms support ACID behavior for the supported single-account operations, but do not claim general serializable isolation; ORM use alone does not establish ACID correctness.

Each mutating request has a stable UUID request ID. PostgreSQL atomically stores the operation and result, detects reuse with a different payload, and retains idempotency records permanently; Redis is not used.

Migrations are one-shot and run only against the Primary. Initial account seeding is also one-shot and must never reset balances after restart or failover.

The intended REST interface is:

- `GET /accounts/{account_id}/balance`
- `POST /accounts/{account_id}/deposit`
- `POST /accounts/{account_id}/withdraw`
- `GET /requests/{request_id}`
- `GET /ready`

`GET /requests/{request_id}` helps determine transaction status, although no record does not necessarily prove non-execution.

`/ready` checks application readiness, database connectivity, and valid Primary status.

Errors use RFC 9457 Problem Details with `application/problem+json`.

Structured JSON logging records requests and their corresponding responses, including request ID, client IP, action, account ID, timestamp, result, and latency.

## High Availability and Fault Tolerance

Patroni manages PostgreSQL HA, and etcd provides distributed coordination, leader election, and Primary ownership. A majority quorum is two of three etcd members.

When an eligible replacement exists, Patroni can automatically fail over; HAProxy selects the ready valid Primary.

Recovery uses Patroni-managed `pg_rewind` where applicable. Automatic destructive replica reinitialization is prohibited: it requires inspection and approval.

PostgreSQL replication is configured as follows:

```yaml
synchronous_mode: on
synchronous_mode_strict: false
synchronous_commit: on
synchronous_node_count: 2
```

Healthy operation targets two synchronous standby acknowledgements. Non-strict mode may relax synchronization when necessary, so it cannot provide a universal zero-data-loss guarantee. Safe Server A-to-Server B failover must be verified through testing.

During a network partition, the system follows a Consistency-First policy:

- Only an authorized Primary serves banking operations.
- Replica reads are unsupported.
- A reachable endpoint returns HTTP 503 when valid service is unavailable.
- The design must prevent two simultaneously authorized writable Primaries.
- Recovery occurs after network connectivity is restored.

Partition verification uses a real network disconnection between VM instances.

## Client and Reliability

The client layer comprises an interactive CLI, load generator, and shared HTTP client; there is no Web GUI.

Requests receive at most three total attempts, use exponential backoff only for transient failures, and reuse the same request ID. The initial overall retry deadline is configurable at 90 seconds.

If the outcome cannot be resolved, the client reports `UNKNOWN`.

Concurrency limits are configurable. Standalone uses Uvicorn concurrency limiting, while HA uses HAProxy traffic and connection management.

Connection or task limits are not necessarily unique-client limits.

## Testing and Verification

Planned pytest coverage includes:

- Unit and integration tests.
- Concurrent requests and lost-update prevention.
- ACID and atomic rollback verification.
- Crash persistence and durability.
- Idempotency verification.
- Automatic failover and recovery.
- Network partition and split-brain prevention.
- Atomicity fault injection between balance mutation and commit.

No test-only production API endpoint is used.

Local VirtualBox infrastructure supports VM power and firewall fault injection. GitHub Actions runs eligible automated tests.

The described tests have not yet been implemented or passed.

## Architectural Limitations

- Non-strict synchronous replication does not universally guarantee zero data loss.
- etcd quorum and PostgreSQL replication acknowledgements are separate guarantees.
- Patroni and etcd run without a hardware Watchdog; `/ready` and other HTTP health checks are not fencing.
- HAProxy on Witness is a single point of failure.
- Four VMs on one physical Host do not protect against physical Host failure.
- Docker Named Volumes provide persistence, not backups.
- Exactly-once claims are limited to durable idempotency, database transactions, and verified failure scenarios.