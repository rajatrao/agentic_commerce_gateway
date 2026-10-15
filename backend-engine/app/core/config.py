"""Application settings, loaded from environment variables (and `.env` locally)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    port: int = 8000

    # Database. Railway injects a `postgresql://` URL; SQLAlchemy needs the driver suffix.
    database_url: str = "postgresql+psycopg2://gateway:gateway@localhost:5432/gateway"
    db_schema: str = "gateway"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_seconds: int = 1800

    # Shopify app credentials (shared with frontend-ui).
    shopify_api_key: str = ""
    shopify_api_secret: str = ""
    shopify_api_version: str = "2026-10"

    # Service JWT minted by frontend-ui (Ed25519). Backend only ever holds the public key.
    jwt_public_key: str = ""
    jwt_issuer: str = "frontend-ui"
    jwt_audience: str = "backend-engine"
    jwt_leeway_seconds: int = 10

    # Reverse direction: backend-engine -> frontend-ui, used to fetch a fresh (auto-refreshed)
    # offline Admin API token. Ed25519; backend holds only this private key.
    backend_jwt_private_key: str = ""
    frontend_internal_url: str = ""

    # Fernet key used to encrypt merchants' offline Admin API tokens at rest.
    token_encryption_key: str = ""

    # Outbound HTTP used by the installation checker.
    http_timeout_seconds: float = 15.0
    http_user_agent: str = "AgenticCommerceGateway-InstallationChecker/0.1"

    # llm.txt feed
    llm_txt_cache_seconds: int = 600
    llm_txt_max_products: int = 2000

    cors_origins: list[str] = Field(default_factory=list)

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        if v.startswith("postgres://"):
            v = "postgresql://" + v.removeprefix("postgres://")
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg2://" + v.removeprefix("postgresql://")
        return v

    @field_validator("jwt_public_key", "backend_jwt_private_key")
    @classmethod
    def _unescape_newlines(cls, v: str) -> str:
        # PEM keys are usually stored in env vars with literal "\n".
        return v.replace("\\n", "\n")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
