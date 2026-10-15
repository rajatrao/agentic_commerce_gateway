"""Application-layer job: pull a merchant's catalog from Shopify, score it, persist the run."""

import uuid

import httpx
import structlog
from sqlmodel import Session

from app.core.config import Settings
from app.core.db import get_engine
from app.models import Merchant
from app.repositories.audits import AuditRepository
from app.repositories.mapping_rules import MappingRuleRepository
from app.services.catalog_auditor import CatalogAuditor
from app.services.shopify_client import PRODUCTS_QUERY, ShopifyAdminClient, ShopifyTokenProvider

log = structlog.get_logger(__name__)


def shopify_client_for(
    merchant: Merchant,
    http: httpx.AsyncClient,
    tokens: ShopifyTokenProvider,
    settings: Settings,
) -> ShopifyAdminClient:
    shop, token_enc = merchant.shop_domain, merchant.access_token_enc

    async def token_getter() -> str:
        return await tokens.get(shop, token_enc)

    return ShopifyAdminClient(
        http,
        shop,
        token_getter,
        settings.shopify_api_version,
        on_unauthorized=lambda: tokens.invalidate(shop),
    )


async def run_catalog_audit(
    run_id: uuid.UUID,
    merchant_id: uuid.UUID,
    http: httpx.AsyncClient,
    tokens: ShopifyTokenProvider,
    settings: Settings,
) -> None:
    with Session(get_engine()) as session:
        audits = AuditRepository(session)
        run = audits.get_run(run_id)
        merchant = session.get(Merchant, merchant_id)
        if run is None or merchant is None:
            return
        try:
            rules = MappingRuleRepository(session).domain_rules(merchant_id)
            client = shopify_client_for(merchant, http, tokens, settings)
            products = client.paginate(PRODUCTS_QUERY, ["products"], {"query": "status:active"})
            summary = await CatalogAuditor(rules).run(
                products, lambda batch: audits.add_scores(run, batch)
            )
            audits.finish_run(run, summary.avg_score)
            log.info(
                "audit.completed",
                shop=merchant.shop_domain,
                products=summary.products_scanned,
                avg_score=str(summary.avg_score),
            )
        except Exception:
            log.exception("audit.failed", shop=merchant.shop_domain, run_id=str(run_id))
            audits.fail_run(run)
