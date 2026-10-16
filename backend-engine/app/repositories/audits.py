import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, any_, delete, func, literal, or_, text, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import AuditRun, ProductAudit, RunStatus
from app.models.base import utcnow
from app.services.catalog_auditor import ProductScore


class LostClaim(Exception):
    """Another worker took this run over (ours stopped heartbeating); stop writing to it."""


@dataclass(frozen=True)
class ClaimedRun:
    """A run this worker owns for one attempt. Writes are fenced on (id, attempt)."""

    id: uuid.UUID
    merchant_id: uuid.UUID
    attempt: int


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def active_run(self, merchant_id: uuid.UUID) -> AuditRun | None:
        """A run that is queued or in progress (a crashed one is reclaimed by the worker)."""
        return (
            await self.session.exec(
                select(AuditRun)
                .where(AuditRun.merchant_id == merchant_id)
                .where(col(AuditRun.status).in_((RunStatus.QUEUED, RunStatus.RUNNING)))
                .order_by(col(AuditRun.started_at).desc())
            )
        ).first()

    async def create_run(self, merchant_id: uuid.UUID) -> AuditRun:
        run = AuditRun(merchant_id=merchant_id, status=RunStatus.QUEUED)
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def get_run(self, run_id: uuid.UUID) -> AuditRun | None:
        return await self.session.get(AuditRun, run_id)

    # ------------------------------------------------------------------ job queue

    async def claim_next(self, *, stale_after: timedelta, max_attempts: int) -> ClaimedRun | None:
        """Take the oldest due run: a queued one, or a running one whose worker went silent.

        `SKIP LOCKED` lets every replica poll the same table without blocking each other. A
        reclaimed run starts over: rows from the abandoned attempt are deleted.
        """
        while True:
            now = utcnow()
            run = (
                await self.session.exec(
                    select(AuditRun)
                    .where(
                        or_(
                            and_(
                                col(AuditRun.status) == RunStatus.QUEUED,
                                col(AuditRun.next_attempt_at) <= now,
                            ),
                            and_(
                                col(AuditRun.status) == RunStatus.RUNNING,
                                or_(
                                    col(AuditRun.heartbeat_at).is_(None),
                                    col(AuditRun.heartbeat_at) < now - stale_after,
                                ),
                            ),
                        )
                    )
                    .order_by(col(AuditRun.next_attempt_at))
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if run is None:
                await self.session.rollback()
                return None
            if run.attempts >= max_attempts:
                # Only a stale `running` run gets here: its last allowed attempt died mid-run.
                run.status, run.finished_at = RunStatus.FAILED, now
                run.last_error = run.last_error or "audit worker stopped responding"
                self.session.add(run)
                await self.session.commit()
                continue
            await self.session.exec(delete(ProductAudit).where(col(ProductAudit.run_id) == run.id))
            run.attempts += 1
            run.status, run.heartbeat_at, run.products_scanned = RunStatus.RUNNING, now, 0
            self.session.add(run)
            await self.session.commit()
            return ClaimedRun(run.id, run.merchant_id, run.attempts)

    async def heartbeat(self, claim: ClaimedRun) -> bool:
        return await self._fenced(claim, heartbeat_at=utcnow())

    async def add_scores(self, claim: ClaimedRun, scores: list[ProductScore]) -> None:
        self.session.add_all(
            ProductAudit(
                run_id=claim.id,
                merchant_id=claim.merchant_id,
                product_gid=s.product_gid,
                title=s.title,
                score=s.score,
                breakdown=s.breakdown,
                missing_fields=s.missing_fields,
                content_hash=s.content_hash,
            )
            for s in scores
        )
        products_scanned = col(AuditRun.products_scanned) + len(scores)
        if not await self._fenced(claim, products_scanned=products_scanned, commit=False):
            await self.session.rollback()
            raise LostClaim(str(claim.id))
        await self.session.commit()

    async def finish_run(self, claim: ClaimedRun, avg_score: Decimal | None) -> None:
        done = await self._fenced(
            claim,
            status=RunStatus.COMPLETED,
            finished_at=utcnow(),
            avg_score=avg_score,
            last_error=None,
        )
        if not done:
            raise LostClaim(str(claim.id))

    async def release(self, claim: ClaimedRun) -> None:
        """Hand an interrupted attempt back to the queue without spending an attempt (used on
        graceful shutdown, so a deploy does not wait for the heartbeat to go stale)."""
        await self.session.rollback()
        await self._fenced(
            claim,
            status=RunStatus.QUEUED,
            attempts=claim.attempt - 1,
            heartbeat_at=None,
            next_attempt_at=utcnow(),
        )

    async def retry_or_fail(
        self, claim: ClaimedRun, error: str, *, retry_in: timedelta | None
    ) -> RunStatus | None:
        """After a failed attempt: requeue it (when `retry_in` is set) or mark it failed."""
        await self.session.rollback()
        values: dict[str, Any] = {"last_error": error[:1000]}
        if retry_in is not None:
            values |= {"status": RunStatus.QUEUED, "next_attempt_at": utcnow() + retry_in}
        else:
            values |= {"status": RunStatus.FAILED, "finished_at": utcnow()}
        if not await self._fenced(claim, **values):
            return None
        return values["status"]  # type: ignore[no-any-return]

    async def _fenced(self, claim: ClaimedRun, *, commit: bool = True, **values: Any) -> bool:
        """Update the run only while this attempt still owns it."""
        result = await self.session.exec(
            update(AuditRun)
            .where(
                col(AuditRun.id) == claim.id,
                col(AuditRun.attempts) == claim.attempt,
                col(AuditRun.status) == RunStatus.RUNNING,
            )
            .values(**values)
        )
        if commit:
            await self.session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ dashboard queries

    async def latest_run(
        self, merchant_id: uuid.UUID, *, completed_only: bool = False
    ) -> AuditRun | None:
        stmt = select(AuditRun).where(AuditRun.merchant_id == merchant_id)
        if completed_only:
            stmt = stmt.where(AuditRun.status == RunStatus.COMPLETED)
        return (await self.session.exec(stmt.order_by(col(AuditRun.started_at).desc()))).first()

    async def grade_counts(self, run_id: uuid.UUID) -> dict[str, int]:
        rows = (
            await self.session.execute(
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
            )
        ).all()
        return {str(g): int(c) for g, c in rows}

    async def top_issues(self, run_id: uuid.UUID, limit: int = 10) -> list[tuple[str, int]]:
        rows = (
            await self.session.execute(
                text(
                    """
                SELECT issue, count(*) AS n
                FROM gateway.product_audits, unnest(missing_fields) AS issue
                WHERE run_id = :run_id
                GROUP BY issue ORDER BY n DESC, issue LIMIT :limit
                """
                ),
                {"run_id": run_id, "limit": limit},
            )
        ).all()
        return [(str(i), int(n)) for i, n in rows]

    async def dimension_averages(self, run_id: uuid.UUID) -> dict[str, dict[str, float]]:
        rows = (
            await self.session.execute(
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
            )
        ).all()
        return {str(k): {"points": round(float(p), 1), "max": float(m)} for k, p, m in rows}

    async def products(
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
        total = (
            await self.session.exec(select(func.count()).select_from(ProductAudit).where(*filters))
        ).one()
        rows = (
            await self.session.exec(
                select(ProductAudit)
                .where(*filters)
                .order_by(col(ProductAudit.score).asc(), col(ProductAudit.title).asc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return rows, int(total)
