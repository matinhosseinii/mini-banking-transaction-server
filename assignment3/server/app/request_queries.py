"""Read-only SQLAlchemy queries for durable request history."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.app.banking import OperationStatus
from server.app.idempotency import Operation
from server.app.models import Request


@dataclass(frozen=True, slots=True)
class RequestPage:
    """A page of request results and its pre-pagination count."""

    items: list[Request]
    total: int


def list_requests(
    session: Session,
    *,
    limit: int,
    offset: int,
    account_id: int | None,
    status: OperationStatus | None,
    operation: Operation | None,
) -> RequestPage:
    """Filter, count, order, and paginate request outcomes in SQL."""
    filters = []
    if account_id is not None:
        filters.append(Request.account_id == account_id)
    if status is not None:
        filters.append(Request.status == status)
    if operation is not None:
        filters.append(Request.operation == operation)

    total = session.scalar(select(func.count()).select_from(Request).where(*filters))
    rows = session.scalars(
        select(Request)
        .where(*filters)
        .order_by(Request.created_at.desc(), Request.request_id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return RequestPage(items=list(rows), total=total or 0)
