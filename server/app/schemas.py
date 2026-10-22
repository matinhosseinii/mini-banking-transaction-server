"""Pydantic schemas exposed by the banking HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from server.app.banking import AccountStatus, BusinessErrorCode, OperationStatus
from server.app.idempotency import Operation

PositiveInteger = Annotated[StrictInt, Field(gt=0)]


class CreateAccountRequest(BaseModel):
    """Input accepted when creating an account."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[StrictStr, Field(max_length=100)]

    @field_validator("name")
    @classmethod
    def name_contains_non_whitespace(cls, value: str) -> str:
        """Reject blank names without altering valid input."""
        if not value.strip():
            raise ValueError("Name must contain non-whitespace.")
        return value


class AmountRequest(BaseModel):
    """Input accepted for a deposit or withdrawal."""

    model_config = ConfigDict(extra="forbid")

    amount: PositiveInteger


class StoredRequestResponse(BaseModel):
    """Public representation of a durable request outcome."""

    request_id: UUID
    operation: Operation
    requested_account_id: int | None
    account_id: int | None
    amount: int | None
    status: OperationStatus
    balance_before: int | None
    balance_after: int | None
    error_code: BusinessErrorCode | None
    created_at: datetime


class MutationResponse(StoredRequestResponse):
    """Mutation outcome, including whether this call was a replay."""

    replayed: bool


class BalanceResponse(BaseModel):
    """Current state of an account."""

    account_id: int
    balance: int
    status: AccountStatus


class RequestHistoryResponse(BaseModel):
    """One page of durable request outcomes."""

    items: list[StoredRequestResponse]
    total: int
    limit: int
    offset: int


class ValidationErrorItem(BaseModel):
    """Sanitized location and message for one invalid input."""

    loc: list[str | int]
    msg: str
    type: str


class ProblemDetails(BaseModel):
    """RFC 9457 problem details plus API-specific extensions."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    error_code: str | None = None
    request_id: UUID | None = None
    replayed: bool | None = None
    errors: list[ValidationErrorItem] | None = None
