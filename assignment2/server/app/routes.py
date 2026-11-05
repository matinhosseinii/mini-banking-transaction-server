"""FastAPI routes for banking operations and request tracking."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, status
from sqlalchemy.orm import Session, sessionmaker

from server.app import banking, idempotency, participant
from server.app.banking import OperationStatus
from server.app.dependencies import get_local_shard, get_session_factory
from server.app.errors import PROBLEM_MEDIA_TYPE, business_problem, problem_response
from server.app.idempotency import CoordinatedResult, Operation
from server.app.request_queries import list_requests
from server.app.schemas import (
    AmountRequest,
    BalanceResponse,
    CreateAccountRequest,
    MutationResponse,
    ParticipantDecisionRequest,
    ParticipantPrepareRequest,
    ParticipantResponse,
    ProblemDetails,
    RequestHistoryResponse,
    StoredRequestResponse,
    TransferRequest,
)
from server.app.sharding import InvalidAccountId, ShardDefinition

router = APIRouter()
PositivePathId = Annotated[int, Path(gt=0)]
IdempotencyKey = Annotated[UUID, Header(alias="Idempotency-Key")]

PROBLEM_RESPONSES = {
    code: {
        "model": ProblemDetails,
        "content": {
            PROBLEM_MEDIA_TYPE: {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"}
            }
        },
    }
    for code in (404, 409, 422, 500, 503)
}

SessionFactory = Annotated[sessionmaker[Session], Depends(get_session_factory)]
LocalShard = Annotated[ShardDefinition, Depends(get_local_shard)]


def _participant_response(
    result: participant.ParticipantResult,
) -> ParticipantResponse:
    return ParticipantResponse(
        transaction_id=result.transaction_id,
        state=result.state,
        vote=result.vote,
        account_id=result.account_id,
        direction=result.direction,
        amount=result.amount,
        balance_before=result.balance_before,
        balance_after=result.balance_after,
        error_code=result.error_code,
    )


def _require_local_owner(shard: ShardDefinition, account_id: int) -> None:
    """Reject foreign-shard IDs while preserving legacy not-found behavior."""
    try:
        shard.require_owner(account_id)
    except InvalidAccountId:
        # Positive integers outside the configured global range retain the
        # existing ACCOUNT_NOT_FOUND contract. Mutations remain durably
        # idempotent because they continue through the coordinator.
        return


def _public_result(result: idempotency.RequestResult) -> dict[str, object]:
    return {
        "request_id": result.request_id,
        "operation": result.operation,
        "requested_account_id": result.requested_account_id,
        "account_id": result.account_id,
        "amount": result.amount,
        "status": result.status,
        "balance_before": result.balance_before,
        "balance_after": result.balance_after,
        "error_code": result.error_code,
        "created_at": result.created_at,
    }


def _mutation_response(
    request: Request, outcome: CoordinatedResult
) -> MutationResponse | object:
    request.state.operation = outcome.result.operation.value
    request.state.business_status = outcome.result.status.value
    request.state.replayed = outcome.replayed
    request.state.account_id = (
        outcome.result.account_id
        if outcome.result.account_id is not None
        else outcome.result.requested_account_id
    )
    if outcome.result.error_code is not None:
        request.state.error_code = outcome.result.error_code.value
    if outcome.result.status is OperationStatus.REJECTED:
        return business_problem(outcome)
    return MutationResponse(**_public_result(outcome.result), replayed=outcome.replayed)


@router.post(
    "/accounts",
    status_code=status.HTTP_201_CREATED,
    response_model=MutationResponse,
    responses=PROBLEM_RESPONSES,
)
def create_account(
    request: Request,
    body: CreateAccountRequest,
    request_id: IdempotencyKey,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> MutationResponse | object:
    return _mutation_response(
        request,
        idempotency.coordinate_create_account(
            session_factory, request_id, body.name, shard
        ),
    )


@router.delete(
    "/accounts/{account_id}",
    response_model=MutationResponse,
    responses=PROBLEM_RESPONSES,
)
def close_account(
    request: Request,
    account_id: PositivePathId,
    request_id: IdempotencyKey,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> MutationResponse | object:
    _require_local_owner(shard, account_id)
    return _mutation_response(
        request,
        idempotency.coordinate_close_account(session_factory, request_id, account_id),
    )


@router.get(
    "/accounts/{account_id}/balance",
    response_model=BalanceResponse,
    responses=PROBLEM_RESPONSES,
)
def get_balance(
    request: Request,
    account_id: PositivePathId,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> BalanceResponse | object:
    _require_local_owner(shard, account_id)
    request.state.account_id = account_id
    with session_factory() as session:
        result = banking.get_balance(session, account_id)
    if result.error_code is not None:
        return problem_response(
            404,
            "Account not found.",
            error_code=result.error_code.value,
        )
    assert result.account_id is not None
    assert result.balance is not None
    assert result.status is not None
    return BalanceResponse(
        account_id=result.account_id,
        balance=result.balance,
        status=result.status,
    )


@router.post(
    "/accounts/{account_id}/deposit",
    response_model=MutationResponse,
    responses=PROBLEM_RESPONSES,
)
def deposit(
    request: Request,
    account_id: PositivePathId,
    body: AmountRequest,
    request_id: IdempotencyKey,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> MutationResponse | object:
    _require_local_owner(shard, account_id)
    return _mutation_response(
        request,
        idempotency.coordinate_deposit(
            session_factory, request_id, account_id, body.amount
        ),
    )


@router.post(
    "/accounts/{account_id}/withdraw",
    response_model=MutationResponse,
    responses=PROBLEM_RESPONSES,
)
def withdraw(
    request: Request,
    account_id: PositivePathId,
    body: AmountRequest,
    request_id: IdempotencyKey,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> MutationResponse | object:
    _require_local_owner(shard, account_id)
    return _mutation_response(
        request,
        idempotency.coordinate_withdraw(
            session_factory, request_id, account_id, body.amount
        ),
    )


@router.post(
    "/transfers",
    response_model=MutationResponse,
    responses=PROBLEM_RESPONSES,
)
def transfer(
    request: Request,
    body: TransferRequest,
    request_id: IdempotencyKey,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> MutationResponse | object:
    _require_local_owner(shard, body.source_account_id)
    _require_local_owner(shard, body.destination_account_id)
    return _mutation_response(
        request,
        idempotency.coordinate_transfer(
            session_factory,
            request_id,
            body.source_account_id,
            body.destination_account_id,
            body.amount,
            shard,
        ),
    )


@router.get(
    "/requests/{request_id}",
    response_model=StoredRequestResponse,
    responses=PROBLEM_RESPONSES,
)
def get_request(
    request: Request,
    request_id: UUID,
    session_factory: SessionFactory,
) -> StoredRequestResponse | object:
    lookup = idempotency.lookup_request(session_factory, request_id)
    if not lookup.found or lookup.result is None:
        return problem_response(404, "Request not found.")
    request.state.account_id = (
        lookup.result.account_id
        if lookup.result.account_id is not None
        else lookup.result.requested_account_id
    )
    return StoredRequestResponse(**_public_result(lookup.result))


@router.get(
    "/requests",
    response_model=RequestHistoryResponse,
    responses=PROBLEM_RESPONSES,
)
def get_requests(
    request: Request,
    session_factory: SessionFactory,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    account_id: Annotated[int | None, Query(gt=0)] = None,
    request_status: Annotated[OperationStatus | None, Query(alias="status")] = None,
    operation: Annotated[Operation | None, Query()] = None,
) -> RequestHistoryResponse:
    request.state.account_id = account_id
    with session_factory() as session:
        page = list_requests(
            session,
            limit=limit,
            offset=offset,
            account_id=account_id,
            status=request_status,
            operation=operation,
        )
    return RequestHistoryResponse(
        items=[
            StoredRequestResponse.model_validate(item, from_attributes=True)
            for item in page.items
        ],
        total=page.total,
        limit=limit,
        offset=offset,
    )


@router.post("/internal/2pc/prepare", response_model=ParticipantResponse)
def prepare_participant(
    body: ParticipantPrepareRequest,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> ParticipantResponse:
    """Prepare one internal shard-local 2PC participant transaction."""
    return _participant_response(
        participant.prepare(
            session_factory,
            shard,
            body.transaction_id,
            body.account_id,
            body.direction,
            body.amount,
        )
    )


@router.post("/internal/2pc/commit", response_model=ParticipantResponse)
def commit_participant(
    body: ParticipantDecisionRequest,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> ParticipantResponse:
    """Commit one prepared internal participant transaction."""
    return _participant_response(
        participant.commit(session_factory, shard, body.transaction_id)
    )


@router.post("/internal/2pc/abort", response_model=ParticipantResponse)
def abort_participant(
    body: ParticipantDecisionRequest,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> ParticipantResponse:
    """Abort one prepared internal participant transaction."""
    return _participant_response(
        participant.abort(session_factory, shard, body.transaction_id)
    )


@router.get("/internal/2pc/{transaction_id}", response_model=ParticipantResponse)
def get_participant(
    transaction_id: UUID,
    session_factory: SessionFactory,
    shard: LocalShard,
) -> ParticipantResponse:
    """Look up one durable internal participant outcome."""
    return _participant_response(
        participant.lookup(session_factory, shard, transaction_id)
    )
