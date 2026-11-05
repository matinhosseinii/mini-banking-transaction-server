"""FastAPI dependencies shared by application routes."""

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session, sessionmaker


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Get the session factory created for this application lifespan."""
    try:
        return request.app.state.session_factory
    except AttributeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not ready.",
        ) from None
