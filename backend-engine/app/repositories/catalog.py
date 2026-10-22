"""Read models for the catalog-intelligence pages: products, findings, history, dashboard."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_, text
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    AttributeSuggestion,
    AuditFinding,
    AuditRun,
    CatalogChange,
    CatalogProduct,
    ProductAudit,
    RunStatus,
)


def _current_findings(merchant_id: uuid.UUID, run_id: uuid.UUID | None) -> list[Any]:
    """Findings that describe the catalog now: the latest completed audit's rule findings plus
    the latest AI findings per product."""
    scope: list[Any] = [AuditFinding.merchant_id == merchant_id]
    if run_id is None:
        scope.append(col(AuditFinding.method) == "ai")
    else:
        scope.append(or_(col(AuditFinding.run_id) == run_id, col(AuditFinding.method) == "ai"))
    return scope


class CatalogRepository:
    """Queries behind the catalog dashboard, product list, findings and change log."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest_completed_run(self, merchant_id: uuid.UUID) -> AuditRun | None:
        """Return the merchant's most recent completed audit run."""
        return (
            await self.session.exec(
                select(AuditRun)
                .where(AuditRun.merchant_id == merchant_id, AuditRun.status == RunStatus.COMPLETED)
                .order_by(col(AuditRun.started_at).desc())
            )
        ).first()

    async def product(self, merchant_id: uuid.UUID, product_gid: str) -> CatalogProduct | None:
        """Return the merchant's snapshot of one product."""
        return (
            await self.session.exec(
                select(CatalogProduct).where(
                    CatalogProduct.merchant_id == merchant_id,
                    CatalogProduct.product_gid == product_gid,
                )
            )
        ).first()

    async def product_audit(self, run_id: uuid.UUID, product_gid: str) -> ProductAudit | None:
        """Return one product's scores in a run."""
        return (
            await self.session.exec(
                select(ProductAudit).where(
                    ProductAudit.run_id == run_id, ProductAudit.product_gid == product_gid
                )
            )
        ).first()

    async def products(
        self,
        merchant_id: uuid.UUID,
        run_id: uuid.UUID | None,
        *,
        schema_key: str | None = None,
        max_readiness: int = 100,
        severity: str | None = None,
        code: str | None = None,
        attribute_key: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[CatalogProduct], int, dict[str, dict[str, int]]]:
        """Page through products with filters, returning the rows, the total and finding counts per
        product."""
        filters: list[Any] = [
            CatalogProduct.merchant_id == merchant_id,
            col(CatalogProduct.removed_at).is_(None),
        ]
        if schema_key:
            filters.append(CatalogProduct.schema_key == schema_key)
        if max_readiness < 100:
            filters.append(func.coalesce(CatalogProduct.readiness_score, 0) <= max_readiness)
        if search:
            filters.append(col(CatalogProduct.title).ilike(f"%{search}%"))
        if severity or code or attribute_key:
            sub = select(AuditFinding.product_gid).where(*_current_findings(merchant_id, run_id))
            if severity:
                sub = sub.where(col(AuditFinding.severity).in_(severity.split(",")))
            if code:
                sub = sub.where(AuditFinding.code == code)
            if attribute_key:
                sub = sub.where(AuditFinding.attribute_key == attribute_key)
            filters.append(col(CatalogProduct.product_gid).in_(sub))
        total = (
            await self.session.exec(
                select(func.count()).select_from(CatalogProduct).where(*filters)
            )
        ).one()
        rows = (
            await self.session.exec(
                select(CatalogProduct)
                .where(*filters)
                .order_by(
                    func.coalesce(CatalogProduct.readiness_score, 0).asc(),
                    col(CatalogProduct.title).asc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
        counts = await self.severity_counts_by_product(
            merchant_id, run_id, [r.product_gid for r in rows]
        )
        return rows, int(total), counts

    async def severity_counts_by_product(
        self, merchant_id: uuid.UUID, run_id: uuid.UUID | None, gids: list[str]
    ) -> dict[str, dict[str, int]]:
        """Count current findings per severity for each of the given products."""
        if not gids:
            return {}
        rows = (
            await self.session.exec(
                select(AuditFinding.product_gid, AuditFinding.severity, func.count())
                .where(*_current_findings(merchant_id, run_id))
                .where(col(AuditFinding.product_gid).in_(gids))
                .group_by(AuditFinding.product_gid, AuditFinding.severity)
            )
        ).all()
        out: dict[str, dict[str, int]] = {}
        for gid, sev, n in rows:
            out.setdefault(gid, {})[getattr(sev, "value", sev)] = int(n)
        return out

    async def findings(
        self,
        merchant_id: uuid.UUID,
        run_id: uuid.UUID | None,
        *,
        product_gid: str | None = None,
        severity: str | None = None,
        code: str | None = None,
        method: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[AuditFinding], int]:
        """Page through the current findings with optional filters."""
        filters = _current_findings(merchant_id, run_id)
        if product_gid:
            filters.append(AuditFinding.product_gid == product_gid)
        if severity:
            filters.append(col(AuditFinding.severity).in_(severity.split(",")))
        if code:
            filters.append(AuditFinding.code == code)
        if method:
            filters.append(AuditFinding.method == method)
        total = (
            await self.session.exec(select(func.count()).select_from(AuditFinding).where(*filters))
        ).one()
        severity_rank = text(
            "array_position(ARRAY['critical','high','medium','low']::text[], severity::text)"
        )
        rows = (
            await self.session.exec(
                select(AuditFinding)
                .where(*filters)
                .order_by(severity_rank, col(AuditFinding.product_title), col(AuditFinding.id))
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return rows, int(total)

    async def finding_summary(
        self, merchant_id: uuid.UUID, run_id: uuid.UUID | None
    ) -> dict[str, Any]:
        """Summarize the current findings by severity and code, plus missing-attribute counts."""
        scope = _current_findings(merchant_id, run_id)
        by_severity = {
            getattr(s, "value", s): int(n)
            for s, n in (
                await self.session.exec(
                    select(AuditFinding.severity, func.count())
                    .where(*scope)
                    .group_by(AuditFinding.severity)
                )
            ).all()
        }
        by_code = [
            {"code": c, "severity": getattr(s, "value", s), "count": int(n)}
            for c, s, n in (
                await self.session.exec(
                    select(AuditFinding.code, func.min(AuditFinding.severity), func.count())
                    .where(*scope)
                    .group_by(AuditFinding.code)
                    .order_by(func.count().desc())
                    .limit(15)
                )
            ).all()
        ]
        missing = (
            await self.session.exec(
                select(
                    func.count(),
                    func.count(func.distinct(AuditFinding.product_gid)).filter(
                        col(AuditFinding.severity).in_(("critical", "high"))
                    ),
                )
                .where(*scope)
                .where(col(AuditFinding.code).in_(("attribute_missing", "attribute_buried")))
            )
        ).one()
        return {
            "by_severity": by_severity,
            "by_code": by_code,
            "missing_attribute_instances": int(missing[0]),
            "products_missing_required": int(missing[1]),
        }

    async def trend(self, merchant_id: uuid.UUID, limit: int = 12) -> Sequence[AuditRun]:
        """Return the merchant's recent completed runs, oldest first, for the score trend."""
        rows = (
            await self.session.exec(
                select(AuditRun)
                .where(AuditRun.merchant_id == merchant_id, AuditRun.status == RunStatus.COMPLETED)
                .order_by(col(AuditRun.started_at).desc())
                .limit(limit)
            )
        ).all()
        return list(reversed(rows))

    async def recent_runs(self, merchant_id: uuid.UUID, limit: int = 20) -> Sequence[AuditRun]:
        """Return the merchant's recent runs, newest first."""
        return (
            await self.session.exec(
                select(AuditRun)
                .where(AuditRun.merchant_id == merchant_id)
                .order_by(col(AuditRun.started_at).desc())
                .limit(limit)
            )
        ).all()

    async def changes(
        self,
        merchant_id: uuid.UUID,
        *,
        product_gid: str | None = None,
        actions: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[CatalogChange], int]:
        """Page through the change log, optionally for one product or some actions."""
        filters: list[Any] = [CatalogChange.merchant_id == merchant_id]
        if product_gid:
            filters.append(CatalogChange.product_gid == product_gid)
        if actions:
            filters.append(col(CatalogChange.action).in_(actions))
        total = (
            await self.session.exec(select(func.count()).select_from(CatalogChange).where(*filters))
        ).one()
        rows = (
            await self.session.exec(
                select(CatalogChange)
                .where(*filters)
                .order_by(col(CatalogChange.created_at).desc(), col(CatalogChange.id).desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return rows, int(total)

    async def change_counts_by_schema(self, merchant_id: uuid.UUID, action: str) -> dict[str, int]:
        """How many changes of one kind were logged per category schema."""
        rows = (
            await self.session.exec(
                select(AttributeSuggestion.schema_key, func.count())
                .select_from(CatalogChange)
                .join(
                    AttributeSuggestion,
                    col(AttributeSuggestion.id) == col(CatalogChange.suggestion_id),
                )
                .where(CatalogChange.merchant_id == merchant_id, CatalogChange.action == action)
                .group_by(AttributeSuggestion.schema_key)
            )
        ).all()
        return {str(k): int(n) for k, n in rows}

    async def change(self, merchant_id: uuid.UUID, change_id: int) -> CatalogChange | None:
        """Load and lock one of the merchant's change log entries."""
        row = await self.session.get(CatalogChange, change_id, with_for_update=True)
        return row if row is not None and row.merchant_id == merchant_id else None

    async def active_product_count(self, merchant_id: uuid.UUID) -> int:
        """Count the merchant's products that have not been removed."""
        return int(
            (
                await self.session.exec(
                    select(func.count())
                    .select_from(CatalogProduct)
                    .where(
                        CatalogProduct.merchant_id == merchant_id,
                        col(CatalogProduct.removed_at).is_(None),
                    )
                )
            ).one()
        )

    async def lowest_readiness_gids(self, merchant_id: uuid.UUID, limit: int) -> list[str]:
        """Return the ids of the products with the lowest readiness scores."""
        return list(
            (
                await self.session.exec(
                    select(CatalogProduct.product_gid)
                    .where(
                        CatalogProduct.merchant_id == merchant_id,
                        col(CatalogProduct.removed_at).is_(None),
                    )
                    .order_by(func.coalesce(CatalogProduct.readiness_score, 0).asc())
                    .limit(limit)
                )
            ).all()
        )

    async def existing_gids(self, merchant_id: uuid.UUID, gids: list[str]) -> list[str]:
        """The subset of `gids` that belong to this merchant's active catalog (tenant check)."""
        found = set(
            (
                await self.session.exec(
                    select(CatalogProduct.product_gid).where(
                        CatalogProduct.merchant_id == merchant_id,
                        col(CatalogProduct.product_gid).in_(gids),
                        col(CatalogProduct.removed_at).is_(None),
                    )
                )
            ).all()
        )
        return [g for g in gids if g in found]
