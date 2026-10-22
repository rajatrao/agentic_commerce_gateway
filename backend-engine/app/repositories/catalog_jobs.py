"""Background catalog jobs: publishing, single-product refreshes and discovery search tests."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import cast, delete, literal
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import col, select

from app.models import CatalogJob, CatalogJobKind, RunStatus
from app.models.base import utcnow
from app.repositories.audits import ClaimedRun, LostClaim
from app.repositories.job_queue import QueueRepository

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)


class CatalogJobRepository(QueueRepository):
    """The catalog job queue, optionally limited to some job kinds."""

    model = CatalogJob
    stale_error = "catalog worker stopped responding"

    def __init__(self, session: Any, kinds: Sequence[CatalogJobKind] | None = None) -> None:
        super().__init__(session)
        self.kinds = tuple(kinds) if kinds else None

    def claim_filters(self) -> list[Any]:
        """Limit claims to this repository's job kinds."""
        return [col(CatalogJob.kind).in_(self.kinds)] if self.kinds else []

    async def create(
        self,
        merchant_id: uuid.UUID,
        kind: CatalogJobKind,
        *,
        params: dict[str, Any] | None = None,
        total: int = 0,
        source: str = "merchant",
        dedupe_key: str | None = None,
        delay: timedelta | None = None,
    ) -> tuple[CatalogJob, bool]:
        """Queue a job. With `dedupe_key`, an already queued job for the same work is returned
        instead (False = not created). A duplicate from a race is harmless: these jobs are
        idempotent."""
        if dedupe_key is not None:
            existing = (
                await self.session.exec(
                    select(CatalogJob).where(
                        CatalogJob.merchant_id == merchant_id,
                        CatalogJob.kind == kind,
                        CatalogJob.dedupe_key == dedupe_key,
                        CatalogJob.status == RunStatus.QUEUED,
                    )
                )
            ).first()
            if existing is not None:
                return existing, False
        job = CatalogJob(
            merchant_id=merchant_id,
            kind=kind,
            params=params or {},
            total=total,
            source=source,
            dedupe_key=dedupe_key,
            next_attempt_at=utcnow() + (delay or timedelta()),
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job, True

    async def get(self, merchant_id: uuid.UUID, job_id: uuid.UUID) -> CatalogJob | None:
        """Return one of the merchant's catalog jobs."""
        job = await self.session.get(CatalogJob, job_id)
        return job if job is not None and job.merchant_id == merchant_id else None

    async def recent(
        self, merchant_id: uuid.UUID, kind: CatalogJobKind, limit: int = 20
    ) -> Sequence[CatalogJob]:
        """Return the merchant's recent jobs of one kind, newest first."""
        return (
            await self.session.exec(
                select(CatalogJob)
                .where(CatalogJob.merchant_id == merchant_id, CatalogJob.kind == kind)
                .order_by(col(CatalogJob.created_at).desc())
                .limit(limit)
            )
        ).all()

    async def active(self, merchant_id: uuid.UUID, kind: CatalogJobKind) -> CatalogJob | None:
        """Return the merchant's queued or running job of one kind, if any."""
        return (
            await self.session.exec(
                select(CatalogJob).where(
                    CatalogJob.merchant_id == merchant_id,
                    CatalogJob.kind == kind,
                    col(CatalogJob.status).in_(ACTIVE),
                )
            )
        ).first()

    async def latest_completed(
        self, merchant_id: uuid.UUID, kind: CatalogJobKind
    ) -> CatalogJob | None:
        """Return the merchant's most recent completed job of one kind."""
        return (
            await self.session.exec(
                select(CatalogJob)
                .where(
                    CatalogJob.merchant_id == merchant_id,
                    CatalogJob.kind == kind,
                    CatalogJob.status == RunStatus.COMPLETED,
                )
                .order_by(col(CatalogJob.finished_at).desc())
            )
        ).first()

    async def progress(
        self,
        claim: ClaimedRun,
        *,
        processed: int = 0,
        succeeded: int = 0,
        failed: int = 0,
        results: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        """Add to the job's counters (and merge `results`) in the attempt's transaction."""
        values: dict[str, Any] = {
            "processed": col(CatalogJob.processed) + processed,
            "succeeded": col(CatalogJob.succeeded) + succeeded,
            "failed": col(CatalogJob.failed) + failed,
            "heartbeat_at": utcnow(),
        }
        if results:
            patch = cast(literal(json.dumps(results, default=str)), JSONB)
            values["results"] = col(CatalogJob.results).op("||")(patch)
        if not await self._fenced(claim, commit=False, **values):
            await self.session.rollback()
            raise LostClaim(str(claim.id))
        if commit:
            await self.session.commit()

    async def complete_refresh(self, claim: ClaimedRun, merchant_id: uuid.UUID, key: str) -> None:
        """A successful refresh leaves no row behind, nor do earlier failed ones for the same
        product: only refreshes that keep failing stay visible."""
        await self.finish(claim)
        await self.session.exec(
            delete(CatalogJob).where(
                col(CatalogJob.merchant_id) == merchant_id,
                col(CatalogJob.kind) == CatalogJobKind.REFRESH,
                col(CatalogJob.dedupe_key) == key,
                col(CatalogJob.status).in_((RunStatus.COMPLETED, RunStatus.FAILED)),
            )
        )
        await self.session.commit()
