"""Application-layer job: pull a merchant's catalog from Shopify, score it, persist the run.

Each product gets the AI Discovery Score (`catalog_auditor`) and a catalog-readiness
assessment against its category schema (`readiness`): findings, a readiness score and a
snapshot of its current structured attributes. The run also records how many products carry
the attributes each configured discovery query needs.
"""

from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.models import Merchant, MerchantStatus
from app.repositories.audits import AuditRepository, ClaimedRun
from app.repositories.catalog_config import CatalogConfig, CatalogConfigRepository
from app.repositories.mapping_rules import MappingRuleRepository
from app.services.catalog_auditor import CatalogAuditor, CatalogSummary, ProductScore
from app.services.readiness import assess_product
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
    config = await CatalogConfigRepository(session).load(merchant)
    stats = RunStats(config)
    client = shopify_client_for(merchant, http, tokens, settings)
    products = client.paginate(PRODUCTS_QUERY, ["products"], {"query": "status:active"})
    summary = await CatalogAuditor(rules, assessor=stats.assess).run(
        products, lambda batch: audits.add_scores(claim, batch)
    )
    await audits.finish_run(claim, summary.avg_score, summary.avg_readiness, stats.as_json())
    return summary


def snapshot(product: dict[str, Any]) -> dict[str, Any]:
    variants = product.get("variants") or {}
    nodes = variants if isinstance(variants, list) else variants.get("nodes") or []
    updated = product.get("updatedAt")
    return {
        "handle": product.get("handle") or "",
        "status": product.get("status") or "ACTIVE",
        "product_type": product.get("productType") or "",
        "vendor": product.get("vendor") or "",
        "category_name": (product.get("category") or {}).get("fullName"),
        "variant_count": len(nodes),
        "shopify_updated_at": _parse_ts(updated) if updated else None,
    }


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class RunStats:
    """Assesses each product and aggregates schema and discovery-query coverage for the run."""

    def __init__(self, config: CatalogConfig) -> None:
        self.config = config
        self.schema_counts: dict[str, int] = defaultdict(int)
        self.query_hits: dict[tuple[str, int], int] = defaultdict(int)
        self.ready = 0

    def assess(self, product: dict[str, Any], scored: ProductScore) -> None:
        readiness = assess_product(
            product,
            scored,
            self.config.schemas,
            self.config.mapping_keys(),
            self.config.weights,
        )
        scored.readiness = readiness
        scored.snapshot = snapshot(product)
        self.schema_counts[readiness.schema_key] += 1
        if readiness.score >= self.config.ready_threshold:
            self.ready += 1
        schema = self.config.schemas[readiness.schema_key]
        for i, query in enumerate(schema.discovery_queries):
            if readiness.supports(query.attributes):
                self.query_hits[(schema.key, i)] += 1

    def as_json(self) -> dict[str, Any]:
        tests = []
        for key, count in self.schema_counts.items():
            schema = self.config.schemas[key]
            for i, query in enumerate(schema.discovery_queries):
                tests.append(
                    {
                        "schema_key": key,
                        "schema_name": schema.name,
                        "query": query.query,
                        "attributes": query.attributes,
                        "products": count,
                        "supported": self.query_hits[(key, i)],
                    }
                )
        return {
            "schemas": dict(self.schema_counts),
            "ready_products": self.ready,
            "ready_threshold": self.config.ready_threshold,
            "discovery_tests": tests,
        }
