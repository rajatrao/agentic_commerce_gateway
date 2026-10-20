"""Import every table so `SQLModel.metadata` is complete for Alembic and tests."""

from app.models.audit import AuditRun, InstallationCheck, ProductAudit, RunStatus
from app.models.base import SCHEMA
from app.models.catalog import (
    AttributeSuggestion,
    AuditFinding,
    CatalogChange,
    CatalogJob,
    CatalogJobKind,
    CatalogProduct,
    CategorySchemaOverride,
    EnrichmentJob,
    MetafieldMapping,
    Severity,
    SuggestionStatus,
)
from app.models.mapping import MappingSource, MetaMappingRule
from app.models.merchant import AgentClient, AgentProtocol, Merchant, MerchantStatus
from app.models.shared_state import LlmTxtCacheEntry, ServiceTokenJti
from app.models.transaction import (
    AgentCart,
    CartStatus,
    IdempotencyRecord,
    WebhookEvent,
)

__all__ = [
    "SCHEMA",
    "AgentCart",
    "AgentClient",
    "AgentProtocol",
    "AttributeSuggestion",
    "AuditFinding",
    "AuditRun",
    "CartStatus",
    "CatalogChange",
    "CatalogJob",
    "CatalogJobKind",
    "CatalogProduct",
    "CategorySchemaOverride",
    "EnrichmentJob",
    "IdempotencyRecord",
    "InstallationCheck",
    "LlmTxtCacheEntry",
    "MappingSource",
    "Merchant",
    "MerchantStatus",
    "MetafieldMapping",
    "MetaMappingRule",
    "ProductAudit",
    "RunStatus",
    "ServiceTokenJti",
    "Severity",
    "SuggestionStatus",
    "WebhookEvent",
]
