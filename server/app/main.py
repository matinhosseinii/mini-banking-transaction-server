"""FastAPI application entry point."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from server.app.config import Settings
from server.app.database import (
    create_database_engine,
    create_session_factory,
    session_scope,
)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Create and dispose application-owned database resources."""
    settings = Settings.from_environment()
    engine = create_database_engine(settings)
    application.state.session_factory = create_session_factory(engine)
    try:
        yield
    finally:
        if hasattr(application.state, "session_factory"):
            del application.state.session_factory
        engine.dispose()


app = FastAPI(lifespan=lifespan)


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Get the session factory created for this application lifespan."""
    try:
        return request.app.state.session_factory
    except AttributeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not ready.",
        ) from None


@app.get("/ready")
def readiness(
    session_factory: Annotated[
        sessionmaker[Session],
        Depends(get_session_factory),
    ],
) -> dict[str, str]:
    """Report whether this standalone node can serve banking requests."""
    try:
        with session_scope(session_factory) as session:
            is_in_recovery = session.execute(
                text("SELECT pg_is_in_recovery()")
            ).scalar_one()
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not ready.",
        ) from None

    # In standalone mode this identifies a writable PostgreSQL node. It does not
    # prove ownership of a Patroni leader lock, which later HA work must verify.
    if is_in_recovery:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not ready.",
        )

    return {"status": "ready"}
