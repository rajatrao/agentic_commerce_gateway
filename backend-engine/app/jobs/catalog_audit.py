"""Application-layer job: pull a merchant's catalog from Shopify, score it, persist the run."""

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.models import Merchant, MerchantStatus
from app.repositories.audits import AuditRepository, ClaimedRun
from app.repositories.mapping_rules import MappingRuleRepository
from app.services.catalog_auditor import CatalogAuditor, CatalogSummary
from app.services.shopify_client import PRODUCTS_QUERY, ShopifyAdminClient, ShopifyTokenProvider


class PermanentAuditError(Exception):
    """Retrying cannot help (the store is gone), so the run fails straight away."""


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
    session: AsyncSession,
    claim: ClaimedRun,
    http: httpx.AsyncClient,
    tokens: ShopifyTokenProvider,
    settings: Settings,
) -> CatalogSummary:
    """One attempt at a claimed run. Every write is fenced on the claim (see `AuditRepository`),
    so an attempt that lost its claim to another worker raises `LostClaim` instead of writing."""
    merchant = await session.get(Merchant, claim.merchant_id)
    if merchant is None or merchant.status is not MerchantStatus.ACTIVE:
        raise PermanentAuditError("store is no longer installed")
    audits = AuditRepository(session)
    rules = await MappingRuleRepository(session).domain_rules(merchant.id)
    client = shopify_client_for(merchant, http, tokens, settings)
    products = client.paginate(PRODUCTS_QUERY, ["products"], {"query": "status:active"})
    summary = await CatalogAuditor(rules).run(
        products, lambda batch: audits.add_scores(claim, batch)
    )
    await audits.finish_run(claim, summary.avg_score)
    return summary
