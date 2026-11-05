"""FastAPI Gateway for shard routing, health checks, and replica failover."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from gateway.config import GatewaySettings, ReplicaConfig
from gateway.replicas import NoHealthyReplica, ReplicaPool
from server.app.sharding import InvalidAccountId, resolve_shard

logger = logging.getLogger("gateway.http")
_FORWARDED_HEADERS = {"content-type", "idempotency-key", "accept"}
_RETURNED_HEADERS = {"content-type", "x-request-id"}


def problem(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": "about:blank",
            "title": "Service Unavailable" if status_code == 503 else "Bad Gateway",
            "status": status_code,
            "detail": detail,
        },
        status_code=status_code,
        media_type="application/problem+json",
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    settings = GatewaySettings.from_environment()
    client = httpx.AsyncClient(follow_redirects=False)
    pool = ReplicaPool(settings.replicas)
    application.state.client = client
    application.state.pool = pool
    application.state.settings = settings
    await _check_all(client, pool, settings)
    task = asyncio.create_task(_health_loop(client, pool, settings))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/ready")
async def ready(request: Request) -> Response:
    snapshot = await request.app.state.pool.snapshot()
    ready_shards = all(any(replicas.values()) for replicas in snapshot.values())
    if not ready_shards:
        return problem(503, "No healthy replica is available for every shard.")
    return JSONResponse({"status": "ready"})


@app.api_route(
    "/accounts",
    methods=["POST"],
)
async def create_account(request: Request) -> Response:
    request_id = _idempotency_key(request)
    if request_id is None:
        return _validation_problem("A valid Idempotency-Key header is required.")
    shard_id = _creation_shard(request_id)
    return await _forward(request, shard_id)


@app.api_route(
    "/accounts/{account_id}",
    methods=["DELETE"],
)
@app.api_route(
    "/accounts/{account_id}/balance",
    methods=["GET"],
)
@app.api_route(
    "/accounts/{account_id}/deposit",
    methods=["POST"],
)
@app.api_route(
    "/accounts/{account_id}/withdraw",
    methods=["POST"],
)
async def account_operation(request: Request, account_id: int) -> Response:
    try:
        shard_id = resolve_shard(account_id).shard_id
    except InvalidAccountId:
        if account_id <= 0:
            return _validation_problem("Account ID must be positive.")
        shard_id = _compatibility_shard(account_id)
    return await _forward(request, shard_id)


@app.post("/transfers")
async def transfer(request: Request) -> Response:
    try:
        document = json.loads(await request.body())
        source_id = document["source_account_id"]
        destination_id = document["destination_account_id"]
        if (
            isinstance(source_id, bool)
            or not isinstance(source_id, int)
            or source_id <= 0
            or isinstance(destination_id, bool)
            or not isinstance(destination_id, int)
            or destination_id <= 0
        ):
            raise ValueError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _validation_problem("Transfer account IDs must be positive integers.")

    try:
        source_shard = resolve_shard(source_id).shard_id
        source_is_supported = True
    except InvalidAccountId:
        source_shard = _compatibility_shard(source_id)
        source_is_supported = False
    try:
        destination_shard = resolve_shard(destination_id).shard_id
        destination_is_supported = True
    except InvalidAccountId:
        destination_shard = source_shard
        destination_is_supported = False
    if (
        source_is_supported
        and destination_is_supported
        and source_shard != destination_shard
    ):
        return _validation_problem(
            "Cross-shard transfers are not implemented.",
            error_code="CROSS_SHARD_TRANSFER_NOT_SUPPORTED",
        )
    return await _forward(request, source_shard)


@app.get("/requests/{request_id}")
async def get_request(request: Request, request_id: UUID) -> Response:
    responses = await _fanout_read(request, ("A", "B"))
    for response in responses:
        if response.status_code == 200:
            return response
    if responses and all(response.status_code == 404 for response in responses):
        return responses[0]
    return problem(503, "Request lookup is unavailable.")


@app.get("/requests")
async def list_requests(request: Request) -> Response:
    account_id = request.query_params.get("account_id")
    if account_id is not None:
        try:
            shard_id = resolve_shard(int(account_id)).shard_id
        except (InvalidAccountId, ValueError):
            return _validation_problem("Account ID is invalid.")
        return await _forward(request, shard_id)

    responses = await _fanout_read(request, ("A", "B"), history=True)
    if len(responses) != 2 or any(
        response.status_code != 200 for response in responses
    ):
        return problem(503, "Request history is unavailable.")
    documents = [json.loads(response.body) for response in responses]
    items = [item for document in documents for item in document["items"]]
    items.sort(key=lambda item: item["created_at"], reverse=True)
    offset = _integer_query(request, "offset", 0)
    limit = _integer_query(request, "limit", 50)
    return JSONResponse(
        {
            "items": items[offset : offset + limit],
            "total": sum(document["total"] for document in documents),
            "limit": limit,
            "offset": offset,
        }
    )


async def _forward(
    request: Request,
    shard_id: str,
    *,
    params: dict[str, str] | None = None,
) -> Response:
    try:
        replica = await request.app.state.pool.select(shard_id)
    except NoHealthyReplica:
        return problem(503, f"No healthy replica is available for shard {shard_id}.")
    return await _send(request, replica, params=params)


async def _send(
    request: Request,
    replica: ReplicaConfig,
    *,
    params: dict[str, str] | None = None,
) -> Response:
    try:
        backend = await request.app.state.client.request(
            request.method,
            f"{replica.base_url}{request.url.path}",
            params=request.query_params if params is None else params,
            content=await request.body(),
            headers={
                name: value
                for name, value in request.headers.items()
                if name.lower() in _FORWARDED_HEADERS
            },
        )
    except httpx.RequestError:
        await request.app.state.pool.set_health(replica.replica_id, False)
        _log_selection(request, replica, 502)
        return problem(
            502,
            "The selected replica did not complete the request; the Gateway did not retry it.",
        )
    _log_selection(request, replica, backend.status_code)
    return Response(
        content=backend.content,
        status_code=backend.status_code,
        headers={
            name: value
            for name, value in backend.headers.items()
            if name.lower() in _RETURNED_HEADERS
        },
    )


async def _fanout_read(
    request: Request, shard_ids: tuple[str, ...], *, history: bool = False
) -> list[Response]:
    params = None
    if history:
        params = dict(request.query_params)
        requested = _integer_query(request, "offset", 0) + _integer_query(
            request, "limit", 50
        )
        params["offset"] = "0"
        params["limit"] = str(min(requested, 100))
    return list(
        await asyncio.gather(
            *(_forward(request, shard, params=params) for shard in shard_ids)
        )
    )


async def _check_all(
    client: httpx.AsyncClient, pool: ReplicaPool, settings: GatewaySettings
) -> None:
    await asyncio.gather(
        *(
            _check_replica(client, pool, replica, settings)
            for replica in settings.replicas
        )
    )


async def _check_replica(
    client: httpx.AsyncClient,
    pool: ReplicaPool,
    replica: ReplicaConfig,
    settings: GatewaySettings,
) -> None:
    try:
        response = await client.get(
            f"{replica.base_url}/ready", timeout=settings.health_timeout_seconds
        )
        healthy = response.status_code == 200
    except httpx.RequestError:
        healthy = False
    await pool.set_health(replica.replica_id, healthy)


async def _health_loop(
    client: httpx.AsyncClient, pool: ReplicaPool, settings: GatewaySettings
) -> None:
    while True:
        await asyncio.sleep(settings.health_interval_seconds)
        await _check_all(client, pool, settings)


def _creation_shard(request_id: UUID) -> str:
    digest = hashlib.sha256(request_id.bytes).digest()
    return ("A", "B")[digest[0] % 2]


def _compatibility_shard(account_id: int) -> str:
    """Route unsupported positive IDs consistently for durable 404 replay."""
    digest = hashlib.sha256(str(account_id).encode("ascii")).digest()
    return ("A", "B")[digest[0] % 2]


def _idempotency_key(request: Request) -> UUID | None:
    value = request.headers.get("Idempotency-Key")
    try:
        return UUID(value) if value is not None else None
    except ValueError:
        return None


def _integer_query(request: Request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except ValueError:
        return default


def _validation_problem(detail: str, *, error_code: str | None = None) -> JSONResponse:
    return JSONResponse(
        {
            "type": "about:blank",
            "title": "Unprocessable Entity",
            "status": 422,
            "detail": detail,
            **({"error_code": error_code} if error_code is not None else {}),
        },
        status_code=422,
        media_type="application/problem+json",
    )


def _log_selection(request: Request, replica: ReplicaConfig, status_code: int) -> None:
    logger.info(
        "Gateway request completed",
        extra={
            "event": "gateway_request_completed",
            "shard_id": replica.shard_id,
            "replica_id": replica.replica_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
        },
    )
