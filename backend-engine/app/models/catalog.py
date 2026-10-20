"""Catalog intelligence tables: product snapshots, findings, schemas, suggestions, write-back.

Shopify stays the source of truth for product data. These tables hold what the app derives
from it (audit findings, readiness scores, AI suggestions, approvals) plus a light snapshot of
each product's identity and current structured attributes so lists and filters do not need a
Shopify round trip. Descriptions are never copied here; enrichment reads them live.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    Identity,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

from app.models.audit import RunStatus
from app.models.base import pg_enum, tz_column, utcnow


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SuggestionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    # A newer enrichment run (or a product change) replaced it before review.
    SUPERSEDED = "superseded"
    PUBLISHED = "published"
    FAILED = "failed"
    REVERTED = "reverted"


def _jsonb(default: Any = None, nullable: bool = False) -> Any:
    if nullable:
        return Field(default=None, sa_column=Column(JSONB, nullable=True))
    server = "[]" if isinstance(default, list) else "{}"
    return Field(
        default_factory=lambda: type(default)(),
        sa_column=Column(JSONB, nullable=False, server_default=server),
    )


class CatalogProduct(SQLModel, table=True):
    """Latest known state of one Shopify product, refreshed by every audit and by webhooks."""

    __tablename__ = "catalog_products"
    __table_args__ = (UniqueConstraint("merchant_id", "product_gid"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    product_gid: str
    title: str = ""
    handle: str = ""
    status: str = "ACTIVE"
    product_type: str = ""
    vendor: str = ""
    category_name: str | None = None
    schema_key: str | None = None
    variant_count: int = 0
    # Current structured values by attribute key: {"capacity": {"value": .., "source": ..}}.
    attributes: dict[str, Any] = _jsonb({})
    readiness_score: int | None = Field(default=None, sa_column=Column(SmallInteger))
    discovery_score: int | None = Field(default=None, sa_column=Column(SmallInteger))
    shopify_updated_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
    synced_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    # Set when the product leaves the active catalog (deleted, archived or drafted).
    removed_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))


class AuditFinding(SQLModel, table=True):
    """One problem with one product. Rule findings belong to an audit run; AI findings to the
    enrichment job that produced them."""

    __tablename__ = "audit_findings"
    __table_args__ = (
        Index("ix_audit_findings_merchant_id_run_id_severity", "merchant_id", "run_id", "severity"),
        Index("ix_audit_findings_merchant_id_product_gid", "merchant_id", "product_gid"),
    )

    id: int | None = Field(default=None, sa_column=Column(BigInteger, Identity(), primary_key=True))
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", ondelete="CASCADE")
    run_id: uuid.UUID | None = Field(
        default=None, foreign_key="audit_runs.id", index=True, ondelete="CASCADE"
    )
    job_id: uuid.UUID | None = Field(
        default=None, foreign_key="enrichment_jobs.id", index=True, ondelete="CASCADE"
    )
    product_gid: str
    product_title: str = ""
    code: str
    attribute_key: str | None = None
    severity: Severity = Field(
        sa_column=Column(pg_enum(Severity, "finding_severity"), nullable=False)
    )
    method: str = "rule"
    message: str = Field(sa_column=Column(Text, nullable=False))
    why_it_matters: str = Field(default="", sa_column=Column(Text, nullable=False))
    recommended_action: str = Field(default="", sa_column=Column(Text, nullable=False))
    evidence: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    confidence: Decimal | None = Field(default=None, sa_column=Column(Numeric(3, 2)))
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())


class CategorySchemaOverride(SQLModel, table=True):
    """A merchant's edited (or added) category schema. Built-in schemas apply until overridden."""

    __tablename__ = "category_schemas"
    __table_args__ = (UniqueConstraint("merchant_id", "key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    key: str
    definition: dict[str, Any] = _jsonb({})
    updated_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())


class MetafieldMapping(SQLModel, table=True):
    """Where an attribute is written in Shopify. Defaults come from the category schema."""

    __tablename__ = "metafield_mappings"
    __table_args__ = (UniqueConstraint("merchant_id", "attribute_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    attribute_key: str
    namespace: str
    key: str
    type: str
    enabled: bool = True
    updated_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())


class EnrichmentJob(SQLModel, table=True):
    """A batch of products to enrich; also its own row in the enrichment job queue."""

    __tablename__ = "enrichment_jobs"
    __table_args__ = (
        Index("ix_enrichment_jobs_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    status: RunStatus = Field(
        default=RunStatus.QUEUED,
        sa_column=Column(pg_enum(RunStatus, "run_status"), nullable=False),
    )
    product_gids: list[str] = Field(
        default_factory=list, sa_column=Column(ARRAY(String), nullable=False, server_default="{}")
    )
    use_ai: bool = False
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    # Per product: {"status": "ok"|"error", "suggestions": n, "error": str|None}.
    results: dict[str, Any] = _jsonb({})
    attempts: int = Field(
        default=0, sa_column=Column(SmallInteger, nullable=False, server_default="0")
    )
    next_attempt_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    heartbeat_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    finished_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))


class CatalogJobKind(StrEnum):
    PUBLISH = "publish"  # write approved suggestions to Shopify
    REFRESH = "refresh"  # re-read and re-score one product
    DISCOVERY = "discovery"  # run the schemas' shopper questions against storefront search


class CatalogJob(SQLModel, table=True):
    """Background catalog work on the same durable queue protocol as audits and enrichment.

    A queued job with a `dedupe_key` absorbs later requests for the same work (for example a
    burst of product webhooks becomes one refresh); a running job does not, so a change that
    arrives mid-run is picked up by the next one.
    """

    __tablename__ = "catalog_jobs"
    __table_args__ = (
        Index("ix_catalog_jobs_status_next_attempt_at", "status", "next_attempt_at"),
        Index("ix_catalog_jobs_merchant_id_kind_created_at", "merchant_id", "kind", "created_at"),
        Index("ix_catalog_jobs_merchant_id_kind_dedupe_key", "merchant_id", "kind", "dedupe_key"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", ondelete="CASCADE")
    kind: CatalogJobKind = Field(
        sa_column=Column(pg_enum(CatalogJobKind, "catalog_job_kind"), nullable=False)
    )
    status: RunStatus = Field(
        default=RunStatus.QUEUED,
        sa_column=Column(pg_enum(RunStatus, "run_status"), nullable=False),
    )
    dedupe_key: str | None = None
    # Who asked: merchant | webhook | publish | revert
    source: str = "merchant"
    params: dict[str, Any] = _jsonb({})
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    results: dict[str, Any] = _jsonb({})
    attempts: int = Field(
        default=0, sa_column=Column(SmallInteger, nullable=False, server_default="0")
    )
    next_attempt_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    heartbeat_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    finished_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))


class AttributeSuggestion(SQLModel, table=True):
    """A proposed value for one attribute of one product, awaiting (or past) merchant review.

    `proposed_value` is NULL when the evidence does not establish a value: the attribute stays
    unknown and the merchant can supply it with "edit and approve".
    """

    __tablename__ = "attribute_suggestions"
    __table_args__ = (
        Index("ix_attribute_suggestions_merchant_id_status", "merchant_id", "status"),
        Index("ix_attribute_suggestions_merchant_id_product_gid", "merchant_id", "product_gid"),
        # At most one open suggestion per product attribute.
        Index(
            "uq_attribute_suggestions_open",
            "merchant_id",
            "product_gid",
            "attribute_key",
            unique=True,
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", ondelete="CASCADE")
    job_id: uuid.UUID | None = Field(
        default=None, foreign_key="enrichment_jobs.id", index=True, ondelete="SET NULL"
    )
    product_gid: str
    product_title: str = ""
    schema_key: str
    attribute_key: str
    data_type: str
    current_value: Any = _jsonb(nullable=True)
    proposed_value: Any = _jsonb(nullable=True)
    evidence: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    evidence_source: str | None = None
    confidence: Decimal = Field(
        default=Decimal("0"), sa_column=Column(Numeric(3, 2), nullable=False)
    )
    method: str
    explanation: str = Field(default="", sa_column=Column(Text, nullable=False))
    validation_status: str = "valid"
    validation_errors: list[str] = Field(
        default_factory=list, sa_column=Column(ARRAY(String), nullable=False, server_default="{}")
    )
    # The rule-based extractor read this same value from the product's own text.
    rule_confirmed: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    status: SuggestionStatus = Field(
        default=SuggestionStatus.PENDING,
        sa_column=Column(pg_enum(SuggestionStatus, "suggestion_status"), nullable=False),
    )
    approved_value: Any = _jsonb(nullable=True)
    reviewed_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
    published_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
    last_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())


class CatalogChange(SQLModel, table=True):
    """Append-only history of review decisions and Shopify writes (and their reverts)."""

    __tablename__ = "catalog_changes"
    __table_args__ = (
        Index("ix_catalog_changes_merchant_id_created_at", "merchant_id", "created_at"),
    )

    id: int | None = Field(default=None, sa_column=Column(BigInteger, Identity(), primary_key=True))
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", ondelete="CASCADE")
    product_gid: str
    product_title: str = ""
    suggestion_id: uuid.UUID | None = Field(
        default=None, foreign_key="attribute_suggestions.id", ondelete="SET NULL"
    )
    attribute_key: str | None = None
    # approve | auto_approve | edit_approve | reject | reopen | publish | publish_failed | revert
    action: str
    before: Any = _jsonb(nullable=True)
    after: Any = _jsonb(nullable=True)
    details: dict[str, Any] = _jsonb({})
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    reverted_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
