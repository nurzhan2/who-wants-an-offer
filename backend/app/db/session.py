"""Async SQLAlchemy engine and session lifecycle.

Everything here is async by design: there is no sync ``Session`` anywhere in
the codebase, and ``create_all`` is never called outside tests.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    future=True,
)

session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that is always closed.

    The session is *not* committed automatically — services decide when a unit
    of work ends, so a read-only request never opens a write transaction.
    """
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close every pooled connection; called from the application lifespan."""
    await engine.dispose()


#: Opens a session of its own, for work that must outlive the request that
#: started it (``app.services.activity``). A dependency rather than a direct use
#: of :data:`session_factory` so a test can hand the work its own transaction.
WorkSessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def get_work_sessions() -> WorkSessions:
    """FastAPI dependency: how background work opens its session."""
    return session_factory
