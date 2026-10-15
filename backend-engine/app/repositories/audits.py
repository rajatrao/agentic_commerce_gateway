import uuid
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import any_, func, literal, text
from sqlmodel import Session, col, select

from app.models import AuditRun, ProductAudit, RunStatus
from app.models.base import utcnow
from app.services.catalog_auditor import ProductScore

STALE_RUN_AFTER = timedelta(minutes=30)


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def active_run(self, merchant_id: uuid.UUID) -> AuditRun | None:
        """A run still in progress (ignoring runs that crashed and were never finished)."""
        return self.session.exec(
            select(AuditRun)
            .where(AuditRun.merchant_id == merchant_id)
            .where(AuditRun.status == RunStatus.RUNNING)
            .where(col(AuditRun.started_at) > utcnow() - STALE_RUN_AFTER)
            .order_by(col(AuditRun.started_at).desc())
        ).first()

    def create_run(self, merchant_id: uuid.UUID) -> AuditRun:
        run = AuditRun(merchant_id=merchant_id)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def get_run(self, run_id: uuid.UUID) -> AuditRun | None:
        return self.session.get(AuditRun, run_id)

    def add_scores(self, run: AuditRun, scores: list[ProductScore]) -> None:
        for s in scores:
            self.session.add(
                ProductAudit(
                    run_id=run.id,
                    merchant_id=run.merchant_id,
                    product_gid=s.product_gid,
                    title=s.title,
                    score=s.score,
                    breakdown=s.breakdown,
                    missing_fields=s.missing_fields,
                    content_hash=s.content_hash,
                )
            )
        run.products_scanned += len(scores)
        self.session.add(run)
        self.session.commit()

    def finish_run(self, run: AuditRun, avg_score: Decimal | None) -> None:
        run.status = RunStatus.COMPLETED
        run.finished_at = utcnow()
        run.avg_score = avg_score
        self.session.add(run)
        self.session.commit()

    def fail_run(self, run: AuditRun) -> None:
        self.session.rollback()
        run.status = RunStatus.FAILED
        run.finished_at = utcnow()
        self.session.add(run)
        self.session.commit()

    def latest_run(
        self, merchant_id: uuid.UUID, *, completed_only: bool = False
    ) -> AuditRun | None:
        stmt = select(AuditRun).where(AuditRun.merchant_id == merchant_id)
        if completed_only:
            stmt = stmt.where(AuditRun.status == RunStatus.COMPLETED)
        return self.session.exec(stmt.order_by(col(AuditRun.started_at).desc())).first()

    def grade_counts(self, run_id: uuid.UUID) -> dict[str, int]:
        rows = self.session.execute(
            text(
                """
                SELECT CASE WHEN score >= 85 THEN 'excellent'
                            WHEN score >= 70 THEN 'good'
                            WHEN score >= 50 THEN 'needs_work'
                            ELSE 'poor' END AS grade,
                       count(*)
                FROM gateway.product_audits WHERE run_id = :run_id GROUP BY 1
                """
            ),
            {"run_id": run_id},
        ).all()
        return {str(g): int(c) for g, c in rows}

    def top_issues(self, run_id: uuid.UUID, limit: int = 10) -> list[tuple[str, int]]:
        rows = self.session.execute(
            text(
                """
                SELECT issue, count(*) AS n
                FROM gateway.product_audits, unnest(missing_fields) AS issue
                WHERE run_id = :run_id
                GROUP BY issue ORDER BY n DESC, issue LIMIT :limit
                """
            ),
            {"run_id": run_id, "limit": limit},
        ).all()
        return [(str(i), int(n)) for i, n in rows]

    def dimension_averages(self, run_id: uuid.UUID) -> dict[str, dict[str, float]]:
        rows = self.session.execute(
            text(
                """
                SELECT d.key,
                       avg((d.value->>'points')::numeric) AS points,
                       max((d.value->>'max')::int) AS max_points
                FROM gateway.product_audits, jsonb_each(breakdown) AS d
                WHERE run_id = :run_id GROUP BY d.key
                """
            ),
            {"run_id": run_id},
        ).all()
        return {str(k): {"points": round(float(p), 1), "max": float(m)} for k, p, m in rows}

    def products(
        self,
        run_id: uuid.UUID,
        *,
        max_score: int = 100,
        issue: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[ProductAudit], int]:
        filters: list[Any] = [ProductAudit.run_id == run_id, ProductAudit.score <= max_score]
        if issue:
            missing = ProductAudit.__table__.c.missing_fields  # type: ignore[attr-defined]
            filters.append(literal(issue) == any_(missing))
        total = self.session.exec(
            select(func.count()).select_from(ProductAudit).where(*filters)
        ).one()
        rows = self.session.exec(
            select(ProductAudit)
            .where(*filters)
            .order_by(col(ProductAudit.score).asc(), col(ProductAudit.title).asc())
            .limit(limit)
            .offset(offset)
        ).all()
        return rows, int(total)
