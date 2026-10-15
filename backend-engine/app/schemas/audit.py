import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class AuditRunOut(BaseModel):
    id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    products_scanned: int
    avg_score: Decimal | None


class IssueCount(BaseModel):
    code: str
    count: int
    message: str


class AuditSummaryOut(BaseModel):
    run: AuditRunOut | None
    """Most recent run, which may still be running."""
    completed_run: AuditRunOut | None
    """Most recent completed run; the summary fields below describe this run."""
    ai_discovery_score: Decimal | None
    grade_counts: dict[str, int]
    dimension_averages: dict[str, dict[str, float]]
    top_issues: list[IssueCount]


class ProductAuditOut(BaseModel):
    product_gid: str
    title: str
    score: int
    breakdown: dict[str, Any]
    missing_fields: list[str]
    issues: list[str]
    admin_url: str


class ProductAuditPage(BaseModel):
    run_id: uuid.UUID
    total: int
    items: list[ProductAuditOut]


class MappingRuleIn(BaseModel):
    source_type: Literal["tag", "metafield", "product_type", "vendor"]
    source_key: str = Field(default="", description="namespace.key for metafields")
    match_pattern: str = Field(min_length=1, max_length=500)
    target_property: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    transform: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    active: bool = True


class MappingRuleOut(MappingRuleIn):
    id: uuid.UUID


class Faq(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    answer: str = Field(min_length=1, max_length=4000)


class FaqList(BaseModel):
    faqs: list[Faq] = Field(max_length=100)
