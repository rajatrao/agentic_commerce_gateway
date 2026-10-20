"""Catalog jobs: background publishing, single-product refreshes and discovery search tests.

All three run on the `catalog_jobs` queue (same protocol as audits and enrichment). Two
workers share it: one for refreshes, which are short and frequent, and one for publishing and
discovery tests, which can run for minutes, so a large publish never delays a refresh.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.jobs.audit_worker import JobQueue, QueueWorker
from app.jobs.catalog_audit import PermanentAuditError, shopify_client_for
from app.models import CatalogJobKind, CatalogProduct, Merchant, MerchantStatus
from app.repositories.audits import ClaimedRun
from app.repositories.catalog_config import CatalogConfigRepository
from app.repositories.catalog_jobs import CatalogJobRepository
from app.services import catalog_publisher
from app.services.discovery_probe import DiscoveryProbe
from app.services.metafield_writer import SET_BATCH, MetafieldWriter
from app.services.product_refresh import refresh_product
from app.services.product_sync import queue_refresh
from app.services.shopify_client import ShopifyTokenProvider
from app.services.taxonomy_writer import TaxonomyWriter


async def _merchant(session: AsyncSession, claim: ClaimedRun) -> Merchant:
    merchant = await session.get(Merchant, claim.merchant_id)
    if merchant is None or merchant.status is not MerchantStatus.ACTIVE:
        raise PermanentAuditError("store is no longer installed")
    return merchant


async def run_publish_job(
    session: AsyncSession,
    claim: ClaimedRun,
    http: httpx.AsyncClient,
    tokens: ShopifyTokenProvider,
    settings: Settings,
) -> None:
    """Write the job's approved suggestions in batches, committing progress after each.

    Resumable: a suggestion leaves `approved` when its batch commits (published or failed),
    so a retried attempt picks up where the last one stopped. Suggestions that were rejected
    or reopened after the job was queued are skipped and listed."""
    merchant = await _merchant(session, claim)
    repo = CatalogJobRepository(session)
    job = await repo.job_state(claim)
    ids = [uuid.UUID(i) for i in job.params.get("suggestion_ids") or []]
    config = await CatalogConfigRepository(session).load(merchant)
    client = shopify_client_for(merchant, http, tokens, settings)
    writer, taxonomy = MetafieldWriter(client), TaxonomyWriter(client)
    touched: set[str] = set()
    while True:
        rows = await catalog_publisher.lock_approved(session, merchant, ids, SET_BATCH)
        if not rows:
            break
        result = await catalog_publisher.publish_rows(session, config, writer, taxonomy, rows)
        details: dict[str, Any] = dict(result.details)
        for entry in result.taxonomy:
            details[f"taxonomy:{entry['product_gid']}:{entry['metafield']}"] = entry
        await repo.progress(
            claim,
            processed=len(rows),
            succeeded=result.published + result.unchanged,
            failed=result.failed,
            results=details,
        )
        touched |= result.product_gids
    job = await repo.job_state(claim)
    skipped = job.total - job.processed
    await repo.finish(
        claim, processed=job.total, results=job.results | {"skipped": max(skipped, 0)}
    )
    for gid in sorted(touched):
        await queue_refresh(session, merchant.id, gid, "publish")


async def run_refresh_job(
    session: AsyncSession,
    claim: ClaimedRun,
    http: httpx.AsyncClient,
    tokens: ShopifyTokenProvider,
    settings: Settings,
) -> None:
    merchant = await _merchant(session, claim)
    repo = CatalogJobRepository(session)
    job = await repo.job_state(claim)
    gid = str(job.params["product_gid"])
    config = await CatalogConfigRepository(session).load(merchant)
    client = shopify_client_for(merchant, http, tokens, settings)
    outcome = await refresh_product(session, merchant, client, config, gid)
    await repo.progress(claim, processed=1, succeeded=1, results=outcome, commit=False)
    await repo.complete_refresh(claim, merchant.id, gid)


async def run_discovery_job(
    session: AsyncSession,
    claim: ClaimedRun,
    http: httpx.AsyncClient,
    tokens: ShopifyTokenProvider,
    settings: Settings,
) -> None:
    """Ask every discovery question of the store's search and record retrieval per question.
    A retried attempt starts over; the questions are cheap and read-only."""
    merchant = await _merchant(session, claim)
    repo = CatalogJobRepository(session)
    config = await CatalogConfigRepository(session).load(merchant)
    products = (
        await session.exec(
            select(CatalogProduct).where(
                CatalogProduct.merchant_id == merchant.id,
                col(CatalogProduct.removed_at).is_(None),
            )
        )
    ).all()
    by_schema: dict[str, list[CatalogProduct]] = {}
    for p in products:
        by_schema.setdefault(p.schema_key or "", []).append(p)
    limit = settings.discovery_result_limit
    probe = DiscoveryProbe(
        http, merchant.shop_domain, shopify_client_for(merchant, http, tokens, settings), limit
    )
    queries: list[dict[str, Any]] = []
    for key, schema in config.schemas.items():
        members = by_schema.get(key) or []
        if not members:
            continue
        schema_gids = {p.product_gid for p in members}
        for question in schema.discovery_queries:
            answerable = {
                p.product_gid: p.title
                for p in members
                if all(
                    (p.attributes.get(a) or {}).get("value") is not None
                    for a in question.attributes
                )
            }
            result = await probe.search(key, question.query, question.attributes)
            measured = result.measure(schema_gids, answerable, limit)
            measured["schema_name"] = schema.name
            queries.append(measured)
            await repo.progress(claim, processed=1, succeeded=0 if result.error else 1)
    recalls = [q["recall"] for q in queries if q["recall"] is not None]
    summary = {
        "method": probe.method,
        "fallback_reason": probe.fallback_reason,
        "result_limit": limit,
        "questions": len(queries),
        "avg_recall": round(sum(recalls) / len(recalls), 2) if recalls else None,
        "queries": queries,
    }
    await repo.finish(claim, total=len(queries), results=summary)


RUNNERS = {
    CatalogJobKind.PUBLISH: run_publish_job,
    CatalogJobKind.REFRESH: run_refresh_job,
    CatalogJobKind.DISCOVERY: run_discovery_job,
}


class CatalogJobWorker(QueueWorker):
    name = "catalog"

    def __init__(
        self,
        db: Database,
        http: httpx.AsyncClient,
        tokens: ShopifyTokenProvider,
        settings: Settings,
        kinds: tuple[CatalogJobKind, ...],
    ) -> None:
        super().__init__(db, http, tokens, settings)
        self.kinds = kinds
        self.name = "catalog_" + "_".join(k.value for k in kinds)

    def queue(self, session: AsyncSession) -> JobQueue:
        return CatalogJobRepository(session, self.kinds)

    async def execute(self, session: AsyncSession, claim: ClaimedRun) -> Any:
        job = await CatalogJobRepository(session).job_state(claim)
        await RUNNERS[job.kind](session, claim, self.http, self.tokens, self.settings)
