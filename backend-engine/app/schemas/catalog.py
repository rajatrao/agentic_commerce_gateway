"""Request and response models for the catalog intelligence routes."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.catalog_schema import AutoApproval, CategorySchema


class RunPoint(BaseModel):
    """Status and averages of one catalog audit run."""

    id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    products_scanned: int
    avg_score: Decimal | None
    avg_readiness: Decimal | None
    last_error: str | None = None


class JobOut(BaseModel):
    """Progress of an enrichment job, with per-product results when requested."""

    id: uuid.UUID
    status: str
    use_ai: bool
    total: int
    succeeded: int
    failed: int
    created_at: datetime
    finished_at: datetime | None
    last_error: str | None
    # Per-product outcomes: all of them on the job endpoint, only failures in listings.
    results: dict[str, Any] = Field(default_factory=dict)
    product_gids: list[str] | None = None


class CatalogJobOut(BaseModel):
    """A publish job or discovery test run, with live progress."""

    id: uuid.UUID
    kind: str
    status: str
    source: str
    total: int
    processed: int
    succeeded: int
    failed: int
    created_at: datetime
    finished_at: datetime | None
    last_error: str | None
    results: dict[str, Any] = Field(default_factory=dict)


class ChangeOut(BaseModel):
    """One entry in the catalog change log (suggestion, review, publish or revert)."""

    id: int
    product_gid: str
    product_title: str
    suggestion_id: uuid.UUID | None = None
    attribute_key: str | None
    action: str
    before: Any
    after: Any
    details: dict[str, Any]
    created_at: datetime
    reverted_at: datetime | None
    revertible: bool


class ChangePage(BaseModel):
    """A page of the catalog change log."""

    total: int
    items: list[ChangeOut]


class OverviewOut(BaseModel):
    """Dashboard metrics. Every number comes from this store's own audit data."""

    has_audit: bool
    products_analyzed: int
    readiness_score: Decimal | None
    discovery_score: Decimal | None
    ready_products: int
    ready_threshold: int
    products_missing_required: int
    missing_attribute_instances: int
    findings_by_severity: dict[str, int]
    top_findings: list[dict[str, Any]]
    suggestions: dict[str, int]
    failed_validations: int
    ai_enabled: bool
    trend: list[RunPoint]
    recent_runs: list[RunPoint]
    recent_jobs: list[JobOut]
    recent_changes: list[ChangeOut]
    discovery_tests: list[dict[str, Any]]
    discovery_run: CatalogJobOut | None = None
    publish_job: CatalogJobOut | None = None


class CatalogProductOut(BaseModel):
    """A product's identity, readiness score and finding counts."""

    product_gid: str
    title: str
    status: str
    product_type: str
    vendor: str
    category_name: str | None
    schema_key: str | None
    readiness_score: int | None
    discovery_score: int | None
    findings: dict[str, int]
    admin_url: str
    synced_at: datetime


class CatalogProductPage(BaseModel):
    """A page of audited products."""

    total: int
    items: list[CatalogProductOut]


class FindingOut(BaseModel):
    """One catalog finding on a product."""

    id: int
    product_gid: str
    product_title: str
    code: str
    attribute_key: str | None
    severity: str
    method: str
    message: str
    why_it_matters: str
    recommended_action: str
    evidence: str | None
    confidence: Decimal | None
    created_at: datetime


class FindingPage(BaseModel):
    """A page of catalog findings."""

    total: int
    items: list[FindingOut]


class SuggestionOut(BaseModel):
    """An attribute value suggested for a product, with its evidence and validation."""

    id: uuid.UUID
    product_gid: str
    product_title: str
    schema_key: str
    attribute_key: str
    attribute_label: str
    data_type: str
    unit: str | None
    allowed_values: list[str] | None
    current_value: Any
    proposed_value: Any
    approved_value: Any
    display_current: str
    display_proposed: str
    evidence: str | None
    evidence_source: str | None
    confidence: Decimal
    method: str
    explanation: str
    validation_status: str
    validation_errors: list[str]
    status: str
    last_error: str | None
    created_at: datetime
    reviewed_at: datetime | None
    published_at: datetime | None


class SuggestionPage(BaseModel):
    """A page of the review queue."""

    total: int
    items: list[SuggestionOut]


class AttributeRow(BaseModel):
    """One schema attribute on a product: its current value, state and pending suggestion."""

    key: str
    label: str
    type: str
    unit: str | None
    importance: str
    required: bool
    current: Any
    current_display: str
    source: str | None
    errors: list[str]
    metafield: str | None


class ProductDetailOut(BaseModel):
    """What the product page shows: readiness, attributes, findings, suggestions and history."""

    product: CatalogProductOut
    schema_name: str | None
    readiness_breakdown: dict[str, Any]
    attributes: list[AttributeRow]
    findings: list[FindingOut]
    suggestions: list[SuggestionOut]
    changes: list[ChangeOut]


class EnrichmentRequest(BaseModel):
    """Which products to enrich, and whether to use the AI provider."""

    product_gids: list[str] = Field(default_factory=list, max_length=1000)
    # Alternative to explicit ids: the N products with the lowest readiness score.
    lowest_readiness: int | None = Field(default=None, ge=1, le=1000)
    use_ai: bool = True


class ReviewItem(BaseModel):
    """One review action on a suggestion."""

    id: uuid.UUID
    action: Literal["approve", "edit", "reject", "reopen"]
    value: Any = None


class ReviewRequest(BaseModel):
    """A batch of review actions."""

    actions: list[ReviewItem] = Field(min_length=1, max_length=500)


class PublishRequest(BaseModel):
    # None: every approved suggestion (up to PUBLISH_MAX_SUGGESTIONS).
    """Which approved suggestions to write to Shopify. `confirm` must be true to start the job."""

    suggestion_ids: list[uuid.UUID] | None = Field(default=None, max_length=5000)
    confirm: bool = False


class AutoApprovalSchemaOut(BaseModel):
    """A schema's auto-approval rule and the attributes it can approve."""

    schema_key: str
    schema_name: str
    rule: AutoApproval
    attributes: list[str]
    pending: int
    qualifying: int
    blocked_by: dict[str, int]
    auto_approved_total: int


class AutoApprovalReport(BaseModel):
    """Auto-approval rules per schema and the suggestions they recently approved."""

    schemas: list[AutoApprovalSchemaOut]
    recent: list[ChangeOut]
    # Current status of each suggestion in `recent`, by suggestion id.
    statuses: dict[str, str] = Field(default_factory=dict)


class SchemaOut(BaseModel):
    """A category schema in effect for the merchant."""

    key: str
    customized: bool
    builtin: bool
    definition: CategorySchema


class MappingOut(BaseModel):
    """Where an attribute is written in Shopify (metafield namespace, key and type)."""

    attribute_key: str
    labels: list[str]
    attribute_type: str
    namespace: str
    key: str
    type: str
    enabled: bool
    source: str
    compatible_types: list[str]
    shopify_definition: str | None = None
    status: str = "unknown"  # ok | will_create | conflict | unknown


class MappingIn(BaseModel):
    """Change where an attribute is written in Shopify."""

    namespace: str = Field(pattern=r"^[a-zA-Z0-9_-]{2,255}$")
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{2,64}$")
    type: str
    enabled: bool = True


class CatalogSettingsOut(BaseModel):
    """The merchant's catalog settings and the defaults they override."""

    readiness_weights: dict[str, int]
    default_weights: dict[str, int]
    ready_threshold: int
    ai_enabled: bool
    ai_provider: str | None = None
    ai_model: str | None
    auto_publish: bool = False
    auto_publish_note: str


class CatalogSettingsIn(BaseModel):
    """Update the readiness weights and the ready threshold."""

    readiness_weights: dict[str, int]
    ready_threshold: int = Field(ge=1, le=100)
