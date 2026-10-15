"""Import every table so `SQLModel.metadata` is complete for Alembic and tests."""

from app.models.audit import AuditRun, InstallationCheck, ProductAudit, RunStatus
from app.models.base import SCHEMA
from app.models.mapping import MappingSource, MetaMappingRule
from app.models.merchant import AgentClient, AgentProtocol, Merchant, MerchantStatus
from app.models.transaction import (
    AgentCart,
    AgentTransaction,
    CartStatus,
    IdempotencyRecord,
    TransactionEvent,
    TransactionStatus,
    WebhookEvent,
)

__all__ = [
    "SCHEMA",
    "AgentCart",
    "AgentClient",
    "AgentProtocol",
    "AgentTransaction",
    "AuditRun",
    "CartStatus",
    "IdempotencyRecord",
    "InstallationCheck",
    "MappingSource",
    "Merchant",
    "MerchantStatus",
    "MetaMappingRule",
    "ProductAudit",
    "RunStatus",
    "TransactionEvent",
    "TransactionStatus",
    "WebhookEvent",
]
