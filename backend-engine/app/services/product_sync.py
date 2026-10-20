"""Keep product snapshots current between audits from Shopify's product webhooks.

products/update refreshes a snapshot's identity fields at once and queues a `refresh` job that
re-reads the product through GraphQL and re-scores it (a burst of webhooks for one product
becomes one refresh). A product leaving the active catalog and products/delete mark the
snapshot removed and close its open suggestions, which can no longer be applied. Webhook
payloads are reduced to identifiers before they are stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import update
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import CatalogJobKind, CatalogProduct
from app.models.base import utcnow
from app.repositories.catalog_jobs import CatalogJobRepository
from app.repositories.enrichment import EnrichmentRepository
from app.repositories.merchants import MerchantRepository

PRODUCT_TOPICS = {"products/update", "products/delete"}

log = structlog.get_logger(__name__)


def product_gid(payload: dict[str, Any]) -> str | None:
    if gid := payload.get("admin_graphql_api_id"):
        return str(gid)
    return f"gid://shopify/Product/{payload['id']}" if payload.get("id") else None


def product_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "admin_graphql_api_id": product_gid(payload),
        "title": payload.get("title"),
        "status": payload.get("status"),
        "updated_at": payload.get("updated_at"),
    }


async def sync_product(
    session: AsyncSession,
    shop: str,
    topic: str,
    payload: dict[str, Any],
    refresh_delay: float = 10.0,
) -> None:
    merchant = await MerchantRepository(session).get_active_by_shop(shop)
    gid = product_gid(payload)
    if merchant is None or gid is None:
        return
    where = (
        col(CatalogProduct.merchant_id) == merchant.id,
        col(CatalogProduct.product_gid) == gid,
    )
    status = str(payload.get("status") or "").upper()
    if topic == "products/delete" or (status and status != "ACTIVE"):
        await session.exec(update(CatalogProduct).where(*where).values(removed_at=utcnow()))
        await EnrichmentRepository(session).supersede_for_product(merchant.id, gid)
        await session.commit()
        log.info("product_sync.removed", shop=shop, product=gid, topic=topic)
        return
    values: dict[str, Any] = {"removed_at": None}
    for field, key in (("title", "title"), ("product_type", "product_type"), ("vendor", "vendor")):
        if payload.get(key) is not None:
            values[field] = payload[key]
    if updated := payload.get("updated_at"):
        values["shopify_updated_at"] = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
    await session.exec(update(CatalogProduct).where(*where).values(**values))
    await session.commit()
    await queue_refresh(session, merchant.id, gid, "webhook", timedelta(seconds=refresh_delay))


async def queue_refresh(
    session: AsyncSession,
    merchant_id: Any,
    gid: str,
    source: str,
    delay: timedelta | None = None,
) -> None:
    await CatalogJobRepository(session).create(
        merchant_id,
        CatalogJobKind.REFRESH,
        params={"product_gid": gid},
        total=1,
        source=source,
        dedupe_key=gid,
        delay=delay,
    )
