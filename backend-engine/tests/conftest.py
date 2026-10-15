"""Test configuration.

Integration tests need Postgres. Set TEST_DATABASE_URL to use an existing server; otherwise a
throwaway container is started with testcontainers (requires Docker).
"""

import os
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet

from tests.keys import make_keypair

PRIVATE_PEM, PUBLIC_PEM = make_keypair()
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
    )
    from app.core.config import get_settings
    from app.core.db import get_engine

    get_settings.cache_clear()
    get_engine.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "migrations")
    )
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    get_engine().dispose()
