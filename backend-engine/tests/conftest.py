"""Test configuration.

Integration tests need Postgres. Set TEST_DATABASE_URL to use an existing server; otherwise a
throwaway container is started with testcontainers (requires Docker).
"""

import os
from collections.abc import Iterator
from functools import lru_cache

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, create_engine

from tests.keys import make_keypair

PRIVATE_PEM, PUBLIC_PEM = make_keypair()
BACKEND_PRIVATE_PEM, BACKEND_PUBLIC_PEM = make_keypair()
FRONTEND_URL = "http://frontend.test:3000"
WEBHOOK_SECRET = "test-shopify-secret"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def app_env(database_url: str) -> Iterator[None]:
    os.environ.update(
        DATABASE_URL=database_url,
        ENVIRONMENT="test",
        JWT_PUBLIC_KEY=PUBLIC_PEM,
        TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        SHOPIFY_API_SECRET=WEBHOOK_SECRET,
        BACKEND_JWT_PRIVATE_KEY=BACKEND_PRIVATE_PEM,
        FRONTEND_INTERNAL_URL=FRONTEND_URL,
    )
    os.environ.update(
        # Retry failed audits at once and poll the queue quickly, so tests do not sleep.
        AUDIT_POLL_SECONDS="0.05",
        AUDIT_RETRY_BACKOFF_SECONDS="0",
        REFRESH_DELAY_SECONDS="0",
    )
    from app.core.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "migrations")
    )
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    sync_engine().dispose()


@lru_cache
def sync_engine() -> Engine:
    """A plain synchronous engine for tests that inspect the database directly."""
    from app.core.config import get_settings

    settings = get_settings()
    return create_engine(
        settings.database_url,
        connect_args={"options": f"-csearch_path={settings.db_schema},public"},
    )
