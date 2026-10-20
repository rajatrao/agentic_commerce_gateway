"""Re-read and re-score one product without waiting for the next full catalog audit.

Runs as a background `refresh` job, queued by the product webhooks and after Shopify writes
and reverts. It updates the product's snapshot (attributes, readiness and discovery scores)
and replaces the product's row and rule findings in the latest completed audit, so product
lists, filters and the product page reflect the change straight away. Store-level averages
and the trend still come from full audits.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import delete, update
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.jobs.catalog_audit import snapshot
from app.models import AuditFinding, CatalogProduct, Merchant, ProductAudit
from app.models.base import utcnow
from app.repositories.audits import finding_rows, product_audit_row, upsert_snapshots
from app.repositories.catalog import CatalogRepository
from app.repositories.catalog_config import CatalogConfig
from app.repositories.enrichment import EnrichmentRepository
from app.repositories.mapping_rules import MappingRuleRepository
from app.services.catalog_auditor import score_product
from app.services.readiness import assess_product
from app.services.shopify_client import PRODUCT_QUERY, ShopifyAdminClient

log = structlog.get_logger(__name__)


async def refresh_product(
    session: AsyncSession,
    merchant: Merchant,
    client: ShopifyAdminClient,
    config: CatalogConfig,
    gid: str,
) -> dict[str, Any]:
    """Returns what happened: {"status": "updated", "readiness": n} or {"status": "removed"}.
    Commits nothing; the caller commits with its job bookkeeping."""
    data = await client.query(PRODUCT_QUERY, {"id": gid})
    product: dict[str, Any] | None = data.get("product")
    if product is None or str(product.get("status") or "ACTIVE").upper() != "ACTIVE":
        await session.exec(
            update(CatalogProduct)
            .where(
                col(CatalogProduct.merchant_id) == merchant.id,
                col(CatalogProduct.product_gid) == gid,
            )
            .values(removed_at=utcnow())
        )
        await EnrichmentRepository(session).supersede_for_product(merchant.id, gid)
        return {"status": "removed"}

    rules = await MappingRuleRepository(session).domain_rules(merchant.id)
    scored = score_product(product, rules)
    scored.readiness = assess_product(
        product, scored, config.schemas, config.mapping_keys(), config.weights
    )
    scored.snapshot = snapshot(product)
    await upsert_snapshots(session, merchant.id, [scored])

    run = await CatalogRepository(session).latest_completed_run(merchant.id)
    if run is not None:
        await session.exec(
            delete(ProductAudit).where(
                col(ProductAudit.run_id) == run.id, col(ProductAudit.product_gid) == gid
            )
        )
        await session.exec(
            delete(AuditFinding).where(
                col(AuditFinding.run_id) == run.id, col(AuditFinding.product_gid) == gid
            )
        )
        session.add(product_audit_row(run.id, merchant.id, scored))
        session.add_all(finding_rows(run.id, merchant.id, scored))
    log.info(
        "product_refresh.updated",
        shop=merchant.shop_domain,
        product=gid,
        readiness=scored.readiness.score,
    )
    return {
        "status": "updated",
        "readiness": scored.readiness.score,
        "discovery": scored.score,
    }
