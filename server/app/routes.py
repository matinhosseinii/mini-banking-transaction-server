"""FastAPI routes for banking operations and request tracking."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, status
from sqlalchemy.orm import Session, sessionmaker

from server.app import banking, idempotency
from server.app.banking import OperationStatus
from server.app.dependencies import get_session_factory
from server.app.errors import PROBLEM_MEDIA_TYPE, business_problem, problem_response
from server.app.idempotency import CoordinatedResult, Operation
from server.app.request_queries import list_requests
from server.app.schemas import (
    AmountRequest,
    BalanceResponse,
    CreateAccountRequest,
    MutationResponse,
    ProblemDetails,
    RequestHistoryResponse,
    StoredRequestResponse,
)

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
) -> MutationResponse | object:
    return _mutation_response(
        request,
        idempotency.coordinate_create_account(session_factory, request_id, body.name),
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
) -> MutationResponse | object:
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
    account_id: PositivePathId,
    session_factory: SessionFactory,
) -> BalanceResponse | object:
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
) -> MutationResponse | object:
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
) -> MutationResponse | object:
    return _mutation_response(
        request,
        idempotency.coordinate_withdraw(
            session_factory, request_id, account_id, body.amount
        ),
    )


@router.get(
    "/requests/{request_id}",
    response_model=StoredRequestResponse,
    responses=PROBLEM_RESPONSES,
)
def get_request(
    request_id: UUID,
    session_factory: SessionFactory,
) -> StoredRequestResponse | object:
    lookup = idempotency.lookup_request(session_factory, request_id)
    if not lookup.found or lookup.result is None:
        return problem_response(404, "Request not found.")
    return StoredRequestResponse(**_public_result(lookup.result))


@router.get(
    "/requests",
    response_model=RequestHistoryResponse,
    responses=PROBLEM_RESPONSES,
)
def get_requests(
    session_factory: SessionFactory,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    account_id: Annotated[int | None, Query(gt=0)] = None,
    request_status: Annotated[OperationStatus | None, Query(alias="status")] = None,
    operation: Annotated[Operation | None, Query()] = None,
) -> RequestHistoryResponse:
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
