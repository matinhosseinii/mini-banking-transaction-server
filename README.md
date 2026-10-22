# Mini Banking Transaction Server

A modular monorepo for a university Distributed Systems project that will provide a banking transaction server and supporting tools.

The mandatory scope covers core banking operations, concurrent request handling, ACID transactions, persistence, and robust error handling.

Advanced Bonus work will add primary/backup clustering, PostgreSQL replication, and automatic failover. Extreme Bonus work will address network partitions and split-brain prevention.

## Technology Stack

- Python 3.12, FastAPI, SQLAlchemy 2.x, and Alembic
- PostgreSQL
- Docker and Docker Compose
- Patroni, etcd, and HAProxy for high availability
- pytest, Ruff, and GitHub Actions

## Repository Layout

- `server/` — Banking API server implementation
- `client/` — Interactive CLI, HTTP client, and load generator
- `infra/` — Docker, deployment, networking, and HA configuration
- `tests/` — Automated test suites
- `docs/` — Documentation, architecture decisions, and verification evidence
- `.github/` — GitHub Actions workflows and repository automation

## Status

Initial development foundation only. No application functionality has been implemented or tested yet.

Setup and execution instructions will be added in later development tasks.

## Client CLI

See the [Command-Only Banking CLI guide](docs/client-cli.md) for installation,
configuration, command reference, retry behavior, and UNKNOWN recovery.

## Concurrent Request Generator

See the [Concurrent Request Generator guide](docs/load-generator.md) for
bounded concurrent balance, deposit, withdrawal, and mixed-operation runs.
