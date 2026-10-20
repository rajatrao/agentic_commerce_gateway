"""Enrichment job: re-read each selected product from Shopify, build validated suggestions."""

from typing import Any

import httpx
import structlog
from sqlalchemy import update
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.jobs.catalog_audit import PermanentAuditError, shopify_client_for
from app.models import CatalogProduct, Merchant, MerchantStatus
from app.models.base import utcnow
from app.repositories.audits import ClaimedRun
from app.repositories.catalog_config import CatalogConfigRepository
from app.repositories.enrichment import EnrichmentRepository
from app.services.ai_enricher import AIEnrichment, AIEnrichmentError, Enricher
from app.services.attribute_extractor import text_sources
from app.services.catalog_schema import match_schema
from app.services.enrichment import build_suggestions
from app.services.shopify_client import (
    PRODUCT_QUERY,
    ShopifyAuthError,
    ShopifyError,
    ShopifyTokenProvider,
)

log = structlog.get_logger(__name__)


async def run_enrichment_job(
    session: AsyncSession,
    claim: ClaimedRun,
    http: httpx.AsyncClient,
    tokens: ShopifyTokenProvider,
    settings: Settings,
    enricher: Enricher | None,
) -> None:
    """One attempt at a claimed job. Products finished by an earlier attempt are skipped, and
    a failure on one product is recorded against that product without failing the job."""
    merchant = await session.get(Merchant, claim.merchant_id)
    if merchant is None or merchant.status is not MerchantStatus.ACTIVE:
        raise PermanentAuditError("store is no longer installed")
    repo = EnrichmentRepository(session)
    job = await repo.job_state(claim)
    config = await CatalogConfigRepository(session).load(merchant)
    client = shopify_client_for(merchant, http, tokens, settings)
    use_ai = job.use_ai and enricher is not None

    for gid in [g for g in job.product_gids if g not in job.results]:
        try:
            data = await client.query(PRODUCT_QUERY, {"id": gid})
        except ShopifyAuthError:
            raise
        except ShopifyError as exc:
            await repo.record_product(claim, gid, "", None, f"Shopify error: {exc}"[:500])
            continue
        product: dict[str, Any] | None = data.get("product")
        if product is None:
            await _mark_removed(session, claim, gid)
            await repo.record_product(claim, gid, "", None, "product no longer exists in Shopify")
            continue

        ai: AIEnrichment | None = None
        ai_error: str | None = None
        if use_ai and enricher is not None:
            schema = match_schema(product, config.schemas)
            metafields = (product.get("metafields") or {}).get("nodes") or []
            try:
                ai = await enricher.enrich(text_sources(product), metafields, schema)
            except AIEnrichmentError as exc:
                ai_error = str(exc)
                log.warning("enrichment.ai_failed", product=gid, error=ai_error)
        result = build_suggestions(
            product,
            config.schemas,
            config.mapping_keys(),
            ai=ai,
            ai_model=enricher.model if use_ai and enricher else None,
            ai_error=ai_error,
        )
        note = f"AI unavailable, used rule-based extraction: {ai_error}" if ai_error else None
        matched = config.schemas.get(result.schema_key)
        await repo.record_product(
            claim,
            gid,
            product.get("title") or "",
            result,
            None,
            note,
            auto_rule=matched.auto_approve if matched else None,
        )
    await repo.finish(claim)


async def _mark_removed(session: AsyncSession, claim: ClaimedRun, gid: str) -> None:
    await session.exec(
        update(CatalogProduct)
        .where(
            col(CatalogProduct.merchant_id) == claim.merchant_id,
            col(CatalogProduct.product_gid) == gid,
        )
        .values(removed_at=utcnow())
    )
    await EnrichmentRepository(session).supersede_for_product(claim.merchant_id, gid)
