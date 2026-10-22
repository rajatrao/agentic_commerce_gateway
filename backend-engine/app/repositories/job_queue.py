"""The durable Postgres job-queue protocol shared by enrichment and catalog jobs.

A job row is claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, heartbeats while it runs and is
retried with backoff. Every write an attempt makes is fenced on (id, attempts, status=running),
so an attempt that lost its claim to another worker cannot overwrite the new attempt's work.
Audit runs follow the same protocol in `AuditRepository`, which also resets partial results.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, ClassVar

from sqlalchemy import and_, or_, update
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import RunStatus
from app.models.base import utcnow
from app.repositories.audits import ClaimedRun, LostClaim


class QueueRepository:
    """Queue operations for a table with the job columns (status, attempts, next_attempt_at,
    heartbeat_at, last_error, finished_at). Subclasses set `model`."""

    model: ClassVar[type[SQLModel]]
    stale_error: ClassVar[str] = "worker stopped responding"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def claim_filters(self) -> list[Any]:
        """Extra conditions on which jobs this queue claims."""
        return []

    async def claim_next(self, *, stale_after: timedelta, max_attempts: int) -> ClaimedRun | None:
        """Take the oldest due job: a queued one, or a running one whose worker went silent.
        A reclaimed job resumes; the job decides what it has already done."""
        m: Any = self.model
        while True:
            now = utcnow()
            job = (
                await self.session.exec(
                    select(m)
                    .where(
                        or_(
                            and_(col(m.status) == RunStatus.QUEUED, col(m.next_attempt_at) <= now),
                            and_(
                                col(m.status) == RunStatus.RUNNING,
                                or_(
                                    col(m.heartbeat_at).is_(None),
                                    col(m.heartbeat_at) < now - stale_after,
                                ),
                            ),
                        ),
                        *self.claim_filters(),
                    )
                    .order_by(col(m.next_attempt_at))
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if job is None:
                await self.session.rollback()
                return None
            if job.attempts >= max_attempts:
                job.status, job.finished_at = RunStatus.FAILED, now
                job.last_error = job.last_error or self.stale_error
                self.session.add(job)
                await self.session.commit()
                continue
            job.attempts += 1
            job.status, job.heartbeat_at = RunStatus.RUNNING, now
            self.session.add(job)
            await self.session.commit()
            return ClaimedRun(job.id, job.merchant_id, job.attempts)

    def _fence(self, claim: ClaimedRun) -> list[Any]:
        m: Any = self.model
        return [
            col(m.id) == claim.id,
            col(m.attempts) == claim.attempt,
            col(m.status) == RunStatus.RUNNING,
        ]

    async def _fenced(self, claim: ClaimedRun, *, commit: bool = True, **values: Any) -> bool:
        """Update the job only while this attempt still owns it."""
        result = await self.session.exec(
            update(self.model).where(*self._fence(claim)).values(**values)
        )
        if commit:
            await self.session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def heartbeat(self, claim: ClaimedRun) -> bool:
        """Record that the claimed job is still being worked on. Returns False if the claim was
        lost."""
        return await self._fenced(claim, heartbeat_at=utcnow())

    async def job_state(self, claim: ClaimedRun) -> Any:
        """Reload the claimed job's row from the database."""
        job = await self.session.get(self.model, claim.id, populate_existing=True)
        assert job is not None
        return job

    async def finish(self, claim: ClaimedRun, **values: Any) -> None:
        """Mark the claimed job completed, raising `LostClaim` if another worker took it over."""
        done = await self._fenced(
            claim, status=RunStatus.COMPLETED, finished_at=utcnow(), last_error=None, **values
        )
        if not done:
            raise LostClaim(str(claim.id))

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

    async def release(self, claim: ClaimedRun) -> None:
        """Hand an interrupted attempt back without spending an attempt (graceful shutdown)."""
        await self.session.rollback()
        await self._fenced(
            claim,
            status=RunStatus.QUEUED,
            attempts=claim.attempt - 1,
            heartbeat_at=None,
            next_attempt_at=utcnow(),
        )
