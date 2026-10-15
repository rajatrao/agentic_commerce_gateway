"""Database engine, connection pooling and the request-scoped session dependency."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, event
from sqlmodel import Session, create_engine

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle_seconds,
        connect_args={"options": f"-csearch_path={settings.db_schema},public"},
    )

    @event.listens_for(engine, "connect")
    def _set_timezone(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        with dbapi_conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")

    return engine


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
