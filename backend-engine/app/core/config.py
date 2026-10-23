"""Application settings, loaded from environment variables (and `.env` locally)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings read from environment variables and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    port: int = 8000

    # Database. Hosting platforms usually provide a `postgresql://` URL; SQLAlchemy needs the driver suffix.
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

    # Catalog audit worker (Postgres job queue over audit_runs; see app/jobs/audit_worker.py).
    audit_worker_enabled: bool = True
    audit_poll_seconds: float = 5.0
    audit_heartbeat_seconds: float = 15.0
    # A running audit whose heartbeat is older than this is assumed dead and claimed again.
    audit_stale_seconds: float = 120.0
    audit_max_attempts: int = 3
    # Retry n waits n * this many seconds.
    audit_retry_backoff_seconds: float = 60.0

    # AI enrichment. Optional: without a provider, enrichment uses rule-based extraction only.
    #   anthropic          Claude via the Anthropic API (needs ANTHROPIC_API_KEY)
    #   ollama             a local Ollama server, e.g. Qwen (AI_BASE_URL, default localhost)
    #   openai_compatible  any OpenAI-compatible /chat/completions API (AI_BASE_URL, AI_API_KEY)
    # Left empty, the provider is anthropic when ANTHROPIC_API_KEY is set, otherwise none.
    ai_provider: Literal["", "none", "anthropic", "ollama", "openai_compatible"] = ""
    # Empty means the provider's default (see DEFAULT_AI_MODELS).
    ai_model: str = ""
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_timeout_seconds: float = 300.0
    anthropic_api_key: str = ""
    # Anthropic only.
    ai_effort: Literal["low", "medium", "high"] = "medium"
    # Enrichment job worker (Postgres job queue over enrichment_jobs).
    enrichment_worker_enabled: bool = True
    enrichment_max_products: int = 250
    # Catalog job worker (Postgres job queue over catalog_jobs): background publishing,
    # single-product refreshes after webhooks and writes, and discovery search tests.
    catalog_worker_enabled: bool = True
    # Approved values written to Shopify per publish job, and per live preview.
    publish_max_suggestions: int = 5000
    publish_preview_max: int = 250
    # Product webhooks within this window are folded into one refresh.
    refresh_delay_seconds: float = 10.0
    # Search results checked per discovery question.
    discovery_result_limit: int = 10

    # Public https origin of this service, used in discovery documents (UCP profile). Falls back
    # to the request's own base URL.
    public_base_url: str = ""

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
        """Whether the app runs with `ENVIRONMENT=production`."""
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings`, loaded once and cached."""
    return Settings()
