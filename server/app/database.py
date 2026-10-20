"""Synchronous SQLAlchemy engine and session-factory helpers."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from server.app.config import Settings


def create_database_engine(settings: Settings) -> Engine:
    """Create an engine without opening a database connection."""
    return create_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=5,
        pool_timeout=30,
        pool_pre_ping=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a reusable factory for short-lived synchronous sessions."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(
    session_factory: sessionmaker[Session],
) -> Generator[Session, None, None]:
    """Yield a session and reliably close it after request-scoped use."""
    with session_factory() as session:
        yield session
