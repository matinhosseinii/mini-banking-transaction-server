# Mini Banking Transaction Server

A client-server banking system developed for a Distributed Systems university assignment.

The project supports concurrent banking operations, ACID transactions, persistent storage, error handling, and structured logging.

## Technology Stack

- Python 3.12, FastAPI, SQLAlchemy, Alembic
- PostgreSQL 17
- HTTPX, asyncio
- Docker Compose
- pytest, Ruff, GitHub Actions

## Features

- Account creation and closure
- Balance inquiry, deposit, and withdrawal
- Concurrent request processing with configurable limits
- ACID transactions and prevention of lost updates
- Durable idempotency and request history
- Validation and structured error responses
- JSON request logging
- Concurrent request generator with balance consistency checks

## Repository Structure

- `server/` — Banking REST API and database implementation
- `client/` — Command-line banking client
- `load_generator/` — Concurrent request generator
- `infra/` — Docker Compose deployment
- `tests/` — Automated tests
- `docs/` — Architecture and usage documentation
- `.github/` — CI workflow

## Deployment

The Banking Server and PostgreSQL run on one VM using Docker Compose. The client runs on a separate VM and communicates with the server over HTTP.

See [Standalone Deployment](docs/standalone-deployment.md) for installation and setup.

## Usage

- [Banking CLI](docs/client-cli.md) — Account operations, balance inquiries, and request history
- [Load Generator](docs/load-generator.md) — Concurrent requests and consistency verification
- [System Architecture](docs/architecture.md) — Implementation details

## Testing

The PostgreSQL test suite passed **257 tests** on the Development Host.

GitHub Actions runs linting, formatting, and dependency checks.