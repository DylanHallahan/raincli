"""Engine and session helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, pool_size=10, max_overflow=10, future=True)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One transaction: commit on success, roll back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
