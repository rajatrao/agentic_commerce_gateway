"""Enrichment job worker: the same durable Postgres queue protocol as catalog audits."""

from typing import Any

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.jobs.audit_worker import JobQueue, QueueWorker
from app.jobs.enrichment import run_enrichment_job
from app.repositories.audits import ClaimedRun
from app.repositories.enrichment import EnrichmentRepository
from app.services.ai_enricher import Enricher
from app.services.shopify_client import ShopifyTokenProvider


class EnrichmentWorker(QueueWorker):
    """Runs queued enrichment jobs."""

    name = "enrichment"

    def __init__(
        self,
        db: Database,
        http: httpx.AsyncClient,
        tokens: ShopifyTokenProvider,
        settings: Settings,
        enricher: Enricher | None,
    ) -> None:
        super().__init__(db, http, tokens, settings)
        self.enricher = enricher

    def queue(self, session: AsyncSession) -> JobQueue:
        """Return the enrichment job queue."""
        return EnrichmentRepository(session)

    async def execute(self, session: AsyncSession, claim: ClaimedRun) -> Any:
        """Run one enrichment job."""
        await run_enrichment_job(
            session, claim, self.http, self.tokens, self.settings, self.enricher
        )
