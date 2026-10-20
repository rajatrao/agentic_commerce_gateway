"""Enrichment job queue and attribute suggestions."""

import json
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, cast, delete, func, literal, or_, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    AttributeSuggestion,
    AuditFinding,
    EnrichmentJob,
    RunStatus,
    Severity,
    SuggestionStatus,
)
from app.models.base import utcnow
from app.repositories.audits import ClaimedRun, LostClaim
from app.services.enrichment import EnrichmentResult

OPEN_STATUSES = (SuggestionStatus.PENDING, SuggestionStatus.APPROVED)


class EnrichmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ jobs

    async def create_job(
        self, merchant_id: uuid.UUID, product_gids: list[str], use_ai: bool
    ) -> EnrichmentJob:
        job = EnrichmentJob(
            merchant_id=merchant_id,
            product_gids=list(dict.fromkeys(product_gids)),
            total=len(set(product_gids)),
            use_ai=use_ai,
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def get_job(self, merchant_id: uuid.UUID, job_id: uuid.UUID) -> EnrichmentJob | None:
        job = await self.session.get(EnrichmentJob, job_id)
        return job if job is not None and job.merchant_id == merchant_id else None

    async def list_jobs(self, merchant_id: uuid.UUID, limit: int = 20) -> Sequence[EnrichmentJob]:
        return (
            await self.session.exec(
                select(EnrichmentJob)
                .where(EnrichmentJob.merchant_id == merchant_id)
                .order_by(col(EnrichmentJob.created_at).desc())
                .limit(limit)
            )
        ).all()

    async def active_job_for(self, merchant_id: uuid.UUID) -> EnrichmentJob | None:
        return (
            await self.session.exec(
                select(EnrichmentJob).where(
                    EnrichmentJob.merchant_id == merchant_id,
                    col(EnrichmentJob.status).in_((RunStatus.QUEUED, RunStatus.RUNNING)),
                )
            )
        ).first()

    async def claim_next(self, *, stale_after: timedelta, max_attempts: int) -> ClaimedRun | None:
        """Same protocol as audit runs: SKIP LOCKED claim, stale heartbeats are reclaimed.
        A reclaimed job resumes: products already finished keep their results."""
        while True:
            now = utcnow()
            job = (
                await self.session.exec(
                    select(EnrichmentJob)
                    .where(
                        or_(
                            and_(
                                col(EnrichmentJob.status) == RunStatus.QUEUED,
                                col(EnrichmentJob.next_attempt_at) <= now,
                            ),
                            and_(
                                col(EnrichmentJob.status) == RunStatus.RUNNING,
                                or_(
                                    col(EnrichmentJob.heartbeat_at).is_(None),
                                    col(EnrichmentJob.heartbeat_at) < now - stale_after,
                                ),
                            ),
                        )
                    )
                    .order_by(col(EnrichmentJob.next_attempt_at))
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if job is None:
                await self.session.rollback()
                return None
            if job.attempts >= max_attempts:
                job.status, job.finished_at = RunStatus.FAILED, now
                job.last_error = job.last_error or "enrichment worker stopped responding"
                self.session.add(job)
                await self.session.commit()
                continue
            job.attempts += 1
            job.status, job.heartbeat_at = RunStatus.RUNNING, now
            self.session.add(job)
            await self.session.commit()
            return ClaimedRun(job.id, job.merchant_id, job.attempts)

    async def _fenced(self, claim: ClaimedRun, *, commit: bool = True, **values: Any) -> bool:
        result = await self.session.exec(
            update(EnrichmentJob)
            .where(
                col(EnrichmentJob.id) == claim.id,
                col(EnrichmentJob.attempts) == claim.attempt,
                col(EnrichmentJob.status) == RunStatus.RUNNING,
            )
            .values(**values)
        )
        if commit:
            await self.session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def heartbeat(self, claim: ClaimedRun) -> bool:
        return await self._fenced(claim, heartbeat_at=utcnow())

    async def job_state(self, claim: ClaimedRun) -> EnrichmentJob:
        job = await self.session.get(EnrichmentJob, claim.id, populate_existing=True)
        assert job is not None
        return job

    async def record_product(
        self,
        claim: ClaimedRun,
        product_gid: str,
        product_title: str,
        result: EnrichmentResult | None,
        error: str | None,
        note: str | None = None,
    ) -> int:
        """Store one product's outcome atomically with its suggestions and AI findings.

        Open suggestions for the product's attributes are superseded by the new ones, except
        approved ones, which wait for publishing and are never silently replaced."""
        created = 0
        if result is not None:
            approved = set(
                (
                    await self.session.exec(
                        select(AttributeSuggestion.attribute_key).where(
                            AttributeSuggestion.merchant_id == claim.merchant_id,
                            AttributeSuggestion.product_gid == product_gid,
                            AttributeSuggestion.status == SuggestionStatus.APPROVED,
                        )
                    )
                ).all()
            )
            await self.session.exec(
                update(AttributeSuggestion)
                .where(
                    col(AttributeSuggestion.merchant_id) == claim.merchant_id,
                    col(AttributeSuggestion.product_gid) == product_gid,
                    col(AttributeSuggestion.status) == SuggestionStatus.PENDING,
                )
                .values(status=SuggestionStatus.SUPERSEDED, updated_at=utcnow())
            )
            for d in result.suggestions:
                if d.attribute_key in approved:
                    continue
                self.session.add(
                    AttributeSuggestion(
                        merchant_id=claim.merchant_id,
                        job_id=claim.id,
                        product_gid=product_gid,
                        product_title=product_title,
                        schema_key=result.schema_key,
                        attribute_key=d.attribute_key,
                        data_type=d.data_type,
                        current_value=d.current_value,
                        proposed_value=d.proposed_value,
                        evidence=d.evidence,
                        evidence_source=d.evidence_source,
                        confidence=d.confidence,
                        method=d.method,
                        explanation=d.explanation,
                        validation_status=d.validation_status,
                        validation_errors=d.validation_errors,
                    )
                )
                created += 1
            # AI findings for this product are replaced by the latest analysis.
            await self.session.exec(
                delete(AuditFinding).where(
                    col(AuditFinding.merchant_id) == claim.merchant_id,
                    col(AuditFinding.product_gid) == product_gid,
                    col(AuditFinding.method) == "ai",
                )
            )
            self.session.add_all(
                AuditFinding(
                    merchant_id=claim.merchant_id,
                    job_id=claim.id,
                    product_gid=product_gid,
                    product_title=product_title,
                    code=f.code,
                    attribute_key=f.attribute_key,
                    severity=Severity(f.severity),
                    method="ai",
                    message=f.message,
                    why_it_matters=f.why_it_matters,
                    recommended_action=f.recommended_action,
                    evidence=f.evidence,
                    confidence=f.confidence,
                )
                for f in result.findings
            )
        entry = {
            "status": "error" if error else "ok",
            "title": product_title,
            "suggestions": created,
            "error": error,
            "note": note,
        }
        patch = cast(literal(json.dumps({product_gid: entry})), JSONB)
        results = col(EnrichmentJob.results).op("||")(patch)
        ok = await self._fenced(
            claim,
            commit=False,
            results=results,
            succeeded=col(EnrichmentJob.succeeded) + (0 if error else 1),
            failed=col(EnrichmentJob.failed) + (1 if error else 0),
            heartbeat_at=utcnow(),
        )
        if not ok:
            await self.session.rollback()
            raise LostClaim(str(claim.id))
        await self.session.commit()
        return created

    async def finish(self, claim: ClaimedRun) -> None:
        if not await self._fenced(claim, status=RunStatus.COMPLETED, finished_at=utcnow()):
            raise LostClaim(str(claim.id))

    async def retry_or_fail(
        self, claim: ClaimedRun, error: str, *, retry_in: timedelta | None
    ) -> None:
        await self.session.rollback()
        values: dict[str, Any] = {"last_error": error[:1000]}
        if retry_in is not None:
            values |= {"status": RunStatus.QUEUED, "next_attempt_at": utcnow() + retry_in}
        else:
            values |= {"status": RunStatus.FAILED, "finished_at": utcnow()}
        await self._fenced(claim, **values)

    async def release(self, claim: ClaimedRun) -> None:
        await self.session.rollback()
        await self._fenced(
            claim,
            status=RunStatus.QUEUED,
            attempts=claim.attempt - 1,
            heartbeat_at=None,
            next_attempt_at=utcnow(),
        )

    # ------------------------------------------------------------------ suggestions

    async def suggestions(
        self,
        merchant_id: uuid.UUID,
        *,
        statuses: Sequence[SuggestionStatus] | None = None,
        product_gid: str | None = None,
        attribute_key: str | None = None,
        validation_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[AttributeSuggestion], int]:
        filters: list[Any] = [AttributeSuggestion.merchant_id == merchant_id]
        if statuses:
            filters.append(col(AttributeSuggestion.status).in_(statuses))
        if product_gid:
            filters.append(AttributeSuggestion.product_gid == product_gid)
        if attribute_key:
            filters.append(AttributeSuggestion.attribute_key == attribute_key)
        if validation_status:
            filters.append(AttributeSuggestion.validation_status == validation_status)
        total = (
            await self.session.exec(
                select(func.count()).select_from(AttributeSuggestion).where(*filters)
            )
        ).one()
        rows = (
            await self.session.exec(
                select(AttributeSuggestion)
                .where(*filters)
                .order_by(
                    col(AttributeSuggestion.product_title),
                    col(AttributeSuggestion.product_gid),
                    col(AttributeSuggestion.attribute_key),
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return rows, int(total)

    async def lock_suggestions(
        self, merchant_id: uuid.UUID, ids: Sequence[uuid.UUID]
    ) -> Sequence[AttributeSuggestion]:
        """Row-lock the merchant's suggestions with these ids (others' ids are ignored)."""
        if not ids:
            return []
        return (
            await self.session.exec(
                select(AttributeSuggestion)
                .where(
                    AttributeSuggestion.merchant_id == merchant_id,
                    col(AttributeSuggestion.id).in_(ids),
                )
                .with_for_update()
            )
        ).all()

    async def status_counts(self, merchant_id: uuid.UUID) -> dict[str, int]:
        rows = (
            await self.session.exec(
                select(AttributeSuggestion.status, func.count())
                .where(AttributeSuggestion.merchant_id == merchant_id)
                .group_by(AttributeSuggestion.status)
            )
        ).all()
        return {str(s.value if hasattr(s, "value") else s): int(n) for s, n in rows}

    async def invalid_open_count(self, merchant_id: uuid.UUID) -> int:
        return int(
            (
                await self.session.exec(
                    select(func.count())
                    .select_from(AttributeSuggestion)
                    .where(
                        AttributeSuggestion.merchant_id == merchant_id,
                        col(AttributeSuggestion.status).in_(OPEN_STATUSES),
                        col(AttributeSuggestion.validation_status).in_(("invalid", "warning")),
                    )
                )
            ).one()
        )

    async def supersede_for_product(self, merchant_id: uuid.UUID, product_gid: str) -> None:
        """The product was deleted: open suggestions can no longer be applied."""
        await self.session.exec(
            update(AttributeSuggestion)
            .where(
                col(AttributeSuggestion.merchant_id) == merchant_id,
                col(AttributeSuggestion.product_gid) == product_gid,
                col(AttributeSuggestion.status).in_(OPEN_STATUSES),
            )
            .values(status=SuggestionStatus.SUPERSEDED, updated_at=utcnow())
        )
