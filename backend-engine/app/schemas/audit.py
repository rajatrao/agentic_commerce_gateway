"""Request and response models for catalog audits, meta-mapping rules and FAQs."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class AuditRunOut(BaseModel):
    """Status and headline numbers of one audit run."""

    id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    products_scanned: int
    avg_score: Decimal | None


class IssueCount(BaseModel):
    """How many products share one audit issue."""

    code: str
    count: int
    message: str


class AuditSummaryOut(BaseModel):
    """The latest audit run plus the AI Discovery Score summary of the latest completed one."""

    run: AuditRunOut | None
    """Most recent run, which may still be running."""
    completed_run: AuditRunOut | None
    """Most recent completed run; the summary fields below describe this run."""
    ai_discovery_score: Decimal | None
    grade_counts: dict[str, int]
    dimension_averages: dict[str, dict[str, float]]
    top_issues: list[IssueCount]


class ProductAuditOut(BaseModel):
    """One product's AI Discovery Score and what it is missing."""

    product_gid: str
    title: str
    score: int
    breakdown: dict[str, Any]
    missing_fields: list[str]
    issues: list[str]
    admin_url: str


class ProductAuditPage(BaseModel):
    """A page of product scores from one audit run."""

    run_id: uuid.UUID
    total: int
    items: list[ProductAuditOut]


class MappingRuleIn(BaseModel):
    """A meta-mapping rule: a regex on a tag, metafield, product type or vendor that sets a product
    property."""

    source_type: Literal["tag", "metafield", "product_type", "vendor"]
    source_key: str = Field(default="", description="namespace.key for metafields")
    match_pattern: str = Field(min_length=1, max_length=500)
    target_property: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    transform: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    active: bool = True


class MappingRuleOut(MappingRuleIn):
    """A stored meta-mapping rule."""

    id: uuid.UUID


class MappingRulePatch(BaseModel):
    """Turn a meta-mapping rule on or off."""

    active: bool


class MappingMatch(BaseModel):
    """One product a meta-mapping rule matched in a preview, with the value it would set."""

    product_gid: str
    title: str
    source_value: str
    value: str
    # A lower-priority-number active rule already sets this property for the product.
    overridden_by_rule: bool = False


class MappingPreviewOut(BaseModel):
    """What a meta-mapping rule would do against the current catalog."""

    products_scanned: int
    matched: int
    matches: list[MappingMatch]
    # Values the rule looked at but did not match, to help write the pattern.
    unmatched_values: list[str]


class Faq(BaseModel):
    """A store FAQ published in `llm.txt`."""

    question: str = Field(min_length=3, max_length=500)
    answer: str = Field(min_length=1, max_length=4000)


class FaqList(BaseModel):
    """The store's FAQs."""

    faqs: list[Faq] = Field(max_length=100)
