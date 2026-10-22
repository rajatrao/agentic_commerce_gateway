"""Storefront installation checks and catalog audit runs with their per-product scores."""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Column, Index, Numeric, SmallInteger, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

from app.models.base import pg_enum, tz_column, utcnow


class RunStatus(StrEnum):
    """Lifecycle of a queued job: queued, running, then completed or failed."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class InstallationCheck(SQLModel, table=True):
    """One storefront installation check: structured data found, app block state and findings."""

    __tablename__ = "installation_checks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    store_url: str
    passed: bool = False
    jsonld_valid: bool = False
    schemas_found: list[str] = Field(
        default_factory=list, sa_column=Column(ARRAY(String), nullable=False, server_default="{}")
    )
    has_product_schema: bool = False
    has_org_schema: bool = False
    app_block_enabled: bool = False
    findings: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )
    checked_at: datetime = Field(default_factory=utcnow, sa_column=tz_column(index=True))


class AuditRun(SQLModel, table=True):
    """A catalog audit, which doubles as its own job row.

    `queued` runs are claimed by `jobs.audit_worker` with `FOR UPDATE SKIP LOCKED`. A `running`
    run whose `heartbeat_at` goes stale (its worker died mid-run) is claimed again, up to the
    worker's attempt limit.
    """

    __tablename__ = "audit_runs"
    __table_args__ = (Index("ix_audit_runs_status_next_attempt_at", "status", "next_attempt_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    status: RunStatus = Field(
        default=RunStatus.QUEUED,
        sa_column=Column(pg_enum(RunStatus, "run_status"), nullable=False),
    )
    started_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    finished_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
    products_scanned: int = 0
    avg_score: Decimal | None = Field(default=None, sa_column=Column(Numeric(5, 2)))
    # Catalog readiness (attribute schema completeness, validity, consistency; see
    # services/readiness.py). Separate from the AI Discovery Score above.
    avg_readiness: Decimal | None = Field(default=None, sa_column=Column(Numeric(5, 2)))
    # Run-level aggregates, e.g. {"discovery_tests": [...], "schemas": {...}}.
    stats: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )
    attempts: int = Field(
        default=0, sa_column=Column(SmallInteger, nullable=False, server_default="0")
    )
    next_attempt_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    heartbeat_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
    last_error: str | None = None


class ProductAudit(SQLModel, table=True):
    """One product's AI Discovery Score and readiness score within an audit run."""

    __tablename__ = "product_audits"
    __table_args__ = (
        Index(
            "ix_product_audits_merchant_product_time", "merchant_id", "product_gid", "audited_at"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(foreign_key="audit_runs.id", index=True, ondelete="CASCADE")
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", ondelete="CASCADE")
    product_gid: str
    title: str
    score: int = Field(sa_column=Column(SmallInteger, nullable=False))
    breakdown: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )
    missing_fields: list[str] = Field(
        default_factory=list, sa_column=Column(ARRAY(String), nullable=False, server_default="{}")
    )
    content_hash: str
    schema_key: str | None = None
    readiness_score: int | None = Field(default=None, sa_column=Column(SmallInteger))
    readiness_breakdown: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )
    audited_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
