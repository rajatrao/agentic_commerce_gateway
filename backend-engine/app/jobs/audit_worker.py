"""Durable catalog-audit worker: a Postgres-backed job queue over `audit_runs`.

Every API replica runs one worker in its lifespan (set `AUDIT_WORKER_ENABLED=false` to run them
elsewhere). Workers claim due runs with `SELECT ... FOR UPDATE SKIP LOCKED`, heartbeat while
they work, and retry failures with backoff. If a process dies mid-run, its heartbeat goes stale
and another worker (or the same one after a restart) claims the run again; writes from the
abandoned attempt are fenced off by the attempt number.
"""

import asyncio
import contextlib
from datetime import timedelta
from typing import Any, Protocol

import httpx
import structlog
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.jobs.catalog_audit import PermanentAuditError, run_catalog_audit
from app.repositories.audits import AuditRepository, ClaimedRun, LostClaim
from app.services.shopify_client import ShopifyAuthError, ShopifyTokenProvider

log = structlog.get_logger(__name__)

# Retrying these cannot succeed until the merchant acts (reinstall, re-grant scopes).
PERMANENT_ERRORS = (PermanentAuditError, ShopifyAuthError)


class JobQueue(Protocol):
    """The repository operations a `QueueWorker` needs (audit runs, enrichment jobs)."""

    async def claim_next(
        self, *, stale_after: timedelta, max_attempts: int
    ) -> ClaimedRun | None: ...
    async def heartbeat(self, claim: ClaimedRun) -> bool: ...
    async def release(self, claim: ClaimedRun) -> None: ...
    async def retry_or_fail(
        self, claim: ClaimedRun, error: str, *, retry_in: timedelta | None
    ) -> Any: ...


class QueueWorker:
    """Claims due jobs from a Postgres-backed queue, heartbeats and retries them."""

    name = "queue"

    def __init__(
        self,
        db: Database,
        http: httpx.AsyncClient,
        tokens: ShopifyTokenProvider,
        settings: Settings,
    ) -> None:
        self.db = db
        self.http = http
        self.tokens = tokens
        self.settings = settings
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def queue(self, session: AsyncSession) -> JobQueue:
        raise NotImplementedError

    async def execute(self, session: AsyncSession, claim: ClaimedRun) -> Any:
        raise NotImplementedError

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"{self.name}-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def wake(self) -> None:
        """Check the queue now instead of at the next poll (a job was just queued here)."""
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                claimed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(f"{self.name}_worker.loop_error")
                claimed = False
            if claimed:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.settings.audit_poll_seconds)
            self._wake.clear()

    async def run_once(self) -> bool:
        """Claim and run one due job. Returns False when the queue had nothing due."""
        async with self.db.session() as session:
            claim = await self.queue(session).claim_next(
                stale_after=timedelta(seconds=self.settings.audit_stale_seconds),
                max_attempts=self.settings.audit_max_attempts,
            )
        if claim is None:
            return False
        await self._attempt(claim)
        return True

    async def _attempt(self, claim: ClaimedRun) -> None:
        log.info(f"{self.name}.started", run_id=str(claim.id), attempt=claim.attempt)
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        try:
            async with self.db.session() as session:
                try:
                    result = await self.execute(session, claim)
                except LostClaim:
                    log.warning(f"{self.name}.claim_lost", run_id=str(claim.id))
                except asyncio.CancelledError:
                    # Shutting down: give the job back so the next worker starts it at once.
                    with contextlib.suppress(Exception):
                        await asyncio.shield(self._release(claim))
                    raise
                except Exception as exc:
                    await self._failed(self.queue(session), claim, exc)
                else:
                    self.completed(claim, result)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    def completed(self, claim: ClaimedRun, result: Any) -> None:
        log.info(f"{self.name}.completed", run_id=str(claim.id))

    async def _failed(self, queue: JobQueue, claim: ClaimedRun, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        retry_in: timedelta | None = None
        if (
            not isinstance(exc, PERMANENT_ERRORS)
            and claim.attempt < self.settings.audit_max_attempts
        ):
            retry_in = timedelta(seconds=self.settings.audit_retry_backoff_seconds * claim.attempt)
        status = await queue.retry_or_fail(claim, error, retry_in=retry_in)
        log.warning(
            f"{self.name}.attempt_failed",
            run_id=str(claim.id),
            attempt=claim.attempt,
            error=error,
            next_status=getattr(status, "value", status),
            exc_info=not isinstance(exc, PERMANENT_ERRORS),
        )

    async def _release(self, claim: ClaimedRun) -> None:
        async with self.db.session() as session:
            await self.queue(session).release(claim)

    async def _heartbeat(self, claim: ClaimedRun) -> None:
        while True:
            await asyncio.sleep(self.settings.audit_heartbeat_seconds)
            try:
                async with self.db.session() as session:
                    if not await self.queue(session).heartbeat(claim):
                        log.warning(f"{self.name}.heartbeat_lost", run_id=str(claim.id))
                        return
            except Exception:
                log.exception(f"{self.name}.heartbeat_failed", run_id=str(claim.id))


class AuditWorker(QueueWorker):
    name = "audit"

    def queue(self, session: AsyncSession) -> JobQueue:
        return AuditRepository(session)

    async def execute(self, session: AsyncSession, claim: ClaimedRun) -> Any:
        return await run_catalog_audit(session, claim, self.http, self.tokens, self.settings)

    def completed(self, claim: ClaimedRun, result: Any) -> None:
        log.info(
            "audit.completed",
            run_id=str(claim.id),
            products=result.products_scanned,
            avg_score=str(result.avg_score),
        )
