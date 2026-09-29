from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def _async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(get_settings().async_database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


@lru_cache
def _sync_sessionmaker() -> sessionmaker[Session]:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return sessionmaker(engine, expire_on_commit=False)


async def async_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async session."""
    async with _async_sessionmaker()() as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Synchronous session for Celery tasks, committed on clean exit."""
    with _sync_sessionmaker()() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
