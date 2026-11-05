# System Architecture

## Overview

Mini Banking Transaction Server uses a client-server architecture with HTTP communication and PostgreSQL for persistent shared state.

The system runs on two separate virtual machines:

- **Server VM:** FastAPI and PostgreSQL 17, deployed with Docker Compose
- **Client VM:** Banking CLI and concurrent request generator

## Banking Server

FastAPI provides the following REST endpoints:

| Method | Endpoint | Description |
|---|---|---|
| POST | `/accounts` | Create account |
| DELETE | `/accounts/{account_id}` | Close account |
| GET | `/accounts/{account_id}/balance` | Get balance |
| POST | `/accounts/{account_id}/deposit` | Deposit |
| POST | `/accounts/{account_id}/withdraw` | Withdraw |
| GET | `/requests/{request_id}` | Get request result |
| GET | `/requests` | List and filter requests |
| GET | `/ready` | Readiness check |

## Database and Transactions

SQLAlchemy manages database access, and Alembic handles schema migrations.

PostgreSQL contains two application tables:

- `accounts` — Account information and balances
- `requests` — Operation results and idempotency records

Balances and transaction amounts use integer values (`BIGINT`).

### ACID Properties

- **Atomicity:** Balance updates and request records commit within the same database transaction. Failures trigger rollback.
- **Consistency:** Input validation and database constraints prevent invalid amounts and negative balances.
- **Isolation:** PostgreSQL READ COMMITTED isolation, row-level locking, and atomic conditional updates prevent lost updates for supported single-account operations.
- **Durability:** Committed transactions are persisted in PostgreSQL before successful responses are returned.

PostgreSQL data is stored in a persistent Docker named volume.

## Concurrency

FastAPI handles concurrent HTTP requests using synchronous endpoints and database connection pooling.

The maximum concurrency is configurable through environment variables and Uvicorn settings.

The load generator uses `asyncio` and HTTPX to execute concurrent banking requests and verify final account balances.

## Idempotency

Mutating operations use UUID request identifiers.

Each operation and its result are stored transactionally in PostgreSQL. Repeated requests with the same identifier and payload return the stored result without applying the operation again.

## Client

The command-line client uses `argparse` and HTTPX.

It supports banking operations, request history, retries for transient failures, and recovery of requests with unknown outcomes.

## Error Handling and Logging

The API returns structured errors using RFC 9457 Problem Details.

HTTP requests produce JSON completion logs containing request ID, client IP, action, account ID when available, timestamp, result, status code, and latency.

The server provides a `/ready` endpoint checked by Docker every 30 seconds.

## Testing

Automated tests cover banking operations, validation, concurrency, rollback, persistence, idempotency, and logging.

The full PostgreSQL test suite passed **257 tests** on the Development Host.

The Standalone deployment has also been verified across two separate VMs.

## Documentation

- [Banking CLI](client-cli.md)
- [Load Generator](load-generator.md)
- [Standalone Deployment](standalone-deployment.md)