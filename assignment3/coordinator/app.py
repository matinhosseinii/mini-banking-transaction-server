"""FastAPI entry point for the durable cross-shard Coordinator."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import create_engine, text

from coordinator.config import Settings
from coordinator.service import Coordinator, CoordinatorConflict, TransferOutcome

PositiveInt = Annotated[StrictInt, Field(gt=0)]
logger = logging.getLogger(__name__)


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_account_id: PositiveInt
    destination_account_id: PositiveInt
    amount: PositiveInt


def _problem(status_code: int, detail: str, **extra: object) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": "Request Failed",
        "status": status_code,
        "detail": detail,
        **extra,
    }
    return JSONResponse(
        body, status_code=status_code, media_type="application/problem+json"
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    settings = Settings.from_environment()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    client = httpx.Client(follow_redirects=False, timeout=5)

    def after_commit(transaction_id: UUID) -> None:
        if transaction_id != settings.crash_after_commit_transaction_id:
            return
        logger.critical(
            "Experiment fault: exiting after durable COMMIT",
            extra={"transaction_id": str(transaction_id)},
        )
        os._exit(86)

    coordinator = Coordinator(
        engine,
        settings.shard_urls,
        client,
        after_commit_hook=(
            after_commit
            if settings.crash_after_commit_transaction_id is not None
            else None
        ),
    )
    application.state.engine = engine
    application.state.coordinator = coordinator

    async def recovery_loop() -> None:
        while True:
            await asyncio.to_thread(coordinator.recover)
            await asyncio.sleep(settings.recovery_interval_seconds)

    await asyncio.to_thread(coordinator.recover)
    task = asyncio.create_task(recovery_loop())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        client.close()
        engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.get("/ready")
def ready(request: Request) -> dict[str, str]:
    with request.app.state.engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.post("/transfers")
def transfer(
    request: Request,
    body: TransferRequest,
    transaction_id: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    if body.source_account_id == body.destination_account_id:
        return _problem(422, "Transfer accounts must be different.")
    try:
        outcome = request.app.state.coordinator.transfer(
            transaction_id,
            body.source_account_id,
            body.destination_account_id,
            body.amount,
        )
    except CoordinatorConflict:
        return _problem(
            409,
            "The Idempotency-Key was already used for a different request.",
            error_code="IDEMPOTENCY_CONFLICT",
            request_id=str(transaction_id),
        )
    except ValueError as error:
        return _problem(422, str(error))
    return _public_response(outcome, mutation=True)


@app.get("/requests/{transaction_id}")
def get_request(request: Request, transaction_id: UUID) -> JSONResponse:
    outcome = request.app.state.coordinator.lookup(transaction_id)
    if outcome is None:
        return _problem(404, "Request not found.")
    return _public_response(outcome, mutation=False)


def _public_response(outcome: TransferOutcome, *, mutation: bool) -> JSONResponse:
    document = {
        "request_id": str(outcome.transaction_id),
        "operation": "TRANSFER",
        "requested_account_id": outcome.source_account_id,
        "account_id": outcome.source_account_id,
        "amount": outcome.amount,
        "status": "SUCCEEDED" if outcome.decision == "COMMIT" else "REJECTED",
        "balance_before": outcome.balance_before,
        "balance_after": outcome.balance_after,
        "error_code": outcome.error_code,
        "created_at": (
            outcome.created_at.isoformat()
            if isinstance(outcome.created_at, datetime)
            else str(outcome.created_at)
        ),
    }
    if mutation:
        document["replayed"] = outcome.replayed
    if outcome.decision == "COMMIT" and outcome.state == "RESOLVED":
        return JSONResponse(document)
    if (
        outcome.decision == "ABORT"
        and outcome.state == "RESOLVED"
        and outcome.error_code is not None
    ):
        if mutation:
            status_code = 404 if outcome.error_code == "ACCOUNT_NOT_FOUND" else 409
            return _problem(
                status_code,
                _error_detail(outcome.error_code),
                error_code=outcome.error_code,
                request_id=str(outcome.transaction_id),
                replayed=outcome.replayed,
            )
        return JSONResponse(document)
    return _problem(
        503,
        "Distributed transaction resolution is still pending.",
        request_id=str(outcome.transaction_id),
    )


def _error_detail(error_code: str) -> str:
    return {
        "ACCOUNT_NOT_FOUND": "Account not found.",
        "ACCOUNT_ALREADY_CLOSED": "Account is already closed.",
        "INSUFFICIENT_FUNDS": "Insufficient funds.",
        "DISTRIBUTED_TRANSFER_ABORTED": "Cross-shard transfer was aborted.",
    }.get(error_code, "Transfer was rejected.")
