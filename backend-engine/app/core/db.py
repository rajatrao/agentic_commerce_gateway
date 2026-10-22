"""Async database engine, session factory and the request-scoped session dependency.

The engine is created in the app lifespan and kept on `app.state.db`, so its connection pool
belongs to the event loop that serves requests (and is disposed with it). Request handlers get
an `AsyncSession` from `get_session`; jobs and the MCP mount open their own with
`db.session()`. Alembic keeps using the synchronous psycopg2 URL in `migrations/env.py`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings


def async_url(database_url: str) -> str:
    """`postgresql+psycopg2://` (what Settings normalises to) -> async psycopg 3 driver."""
    for prefix in ("postgresql+psycopg2://", "postgresql://"):
        if database_url.startswith(prefix):
            return "postgresql+psycopg://" + database_url.removeprefix(prefix)
    return database_url


@dataclass(frozen=True)
class Database:
    """The async engine and its session factory, created once per app."""

    engine: AsyncEngine
    session: async_sessionmaker[AsyncSession]

    async def dispose(self) -> None:
        """Close every pooled connection."""
        await self.engine.dispose()


def create_database(settings: Settings) -> Database:
    """Create the async engine (gateway schema search path, UTC) and its session factory."""
    engine = create_async_engine(
        async_url(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle_seconds,
        connect_args={"options": f"-csearch_path={settings.db_schema},public -ctimezone=UTC"},
    )
    # expire_on_commit=False: async sessions cannot lazy-load, so objects stay readable after
    # commit. Repositories `refresh()` explicitly where server-side values matter.
    return Database(engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields one `AsyncSession` per request."""
    db: Database = request.app.state.db
    async with db.session() as session:
        yield session
