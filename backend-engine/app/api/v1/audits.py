"""Catalog audit routes (AI Discovery Score), called by frontend-ui for a merchant."""

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import (
    CurrentMerchant,
    HttpClientDep,
    SessionDep,
    SettingsDep,
    TokenProviderDep,
    require_service_scopes,
)
from app.jobs.audit_worker import AuditWorker
from app.jobs.catalog_audit import shopify_client_for
from app.models import AuditRun, MappingSource, MetaMappingRule
from app.repositories.audits import AuditRepository
from app.repositories.mapping_rules import MappingRuleRepository
from app.repositories.shared_state import LlmTxtCacheRepository
from app.schemas.audit import (
    AuditRunOut,
    AuditSummaryOut,
    FaqList,
    IssueCount,
    MappingPreviewOut,
    MappingRuleIn,
    MappingRuleOut,
    MappingRulePatch,
    ProductAuditOut,
    ProductAuditPage,
)
from app.services.catalog_auditor import ISSUE_MESSAGES
from app.services.shopify_client import MAPPING_PREVIEW_QUERY, ShopifyAuthError, ShopifyError
from app.services.tag_mapper import MappingRule, preview_rule

router = APIRouter(prefix="/merchant", tags=["catalog"])
write = [Depends(require_service_scopes("merchant:write"))]


def _run_out(run: AuditRun | None) -> AuditRunOut | None:
    if run is None:
        return None
    return AuditRunOut(
        id=run.id,
        status=run.status.value,
        started_at=run.started_at,
        finished_at=run.finished_at,
        products_scanned=run.products_scanned,
        avg_score=run.avg_score,
    )


@router.post(
    "/audits",
    response_model=AuditRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=write,
)
async def start_audit(
    merchant: CurrentMerchant, session: SessionDep, request: Request
) -> AuditRunOut:
    """Queue an audit. `jobs.audit_worker` (on any replica) runs it; a restart mid-run only
    delays it, because a run whose worker stops heartbeating is picked up again."""
    audits = AuditRepository(session)
    existing = await audits.active_run(merchant.id)
    if existing is not None:
        return _run_out(existing)  # type: ignore[return-value]
    run = await audits.create_run(merchant.id)
    worker: AuditWorker | None = getattr(request.app.state, "audit_worker", None)
    if worker is not None:
        worker.wake()
    return _run_out(run)  # type: ignore[return-value]


@router.get("/audits/latest", response_model=AuditSummaryOut)
async def latest_audit(merchant: CurrentMerchant, session: SessionDep) -> AuditSummaryOut:
    """Return the latest audit run and the summary of the latest completed one."""
    audits = AuditRepository(session)
    latest = await audits.latest_run(merchant.id)
    completed = await audits.latest_run(merchant.id, completed_only=True)
    if completed is None:
        return AuditSummaryOut(
            run=_run_out(latest),
            completed_run=None,
            ai_discovery_score=None,
            grade_counts={},
            dimension_averages={},
            top_issues=[],
        )
    return AuditSummaryOut(
        run=_run_out(latest),
        completed_run=_run_out(completed),
        ai_discovery_score=completed.avg_score,
        grade_counts=await audits.grade_counts(completed.id),
        dimension_averages=await audits.dimension_averages(completed.id),
        top_issues=[
            IssueCount(code=c, count=n, message=ISSUE_MESSAGES.get(c, c))
            for c, n in await audits.top_issues(completed.id)
        ],
    )


def _admin_url(shop: str, gid: str) -> str:
    match = re.search(r"/(\d+)$", gid)
    store = shop.removesuffix(".myshopify.com")
    return f"https://admin.shopify.com/store/{store}/products/{match.group(1) if match else ''}"


@router.get("/audits/latest/products", response_model=ProductAuditPage)
async def latest_audit_products(
    merchant: CurrentMerchant,
    session: SessionDep,
    max_score: int = Query(100, ge=0, le=100),
    issue: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
) -> ProductAuditPage:
    """Page through the latest completed audit's per-product scores, filtered by score or issue."""
    audits = AuditRepository(session)
    completed = await audits.latest_run(merchant.id, completed_only=True)
    if completed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no completed audit yet")
    rows, total = await audits.products(
        completed.id, max_score=max_score, issue=issue, limit=limit, offset=offset
    )
    return ProductAuditPage(
        run_id=completed.id,
        total=total,
        items=[
            ProductAuditOut(
                product_gid=r.product_gid,
                title=r.title,
                score=r.score,
                breakdown=r.breakdown,
                missing_fields=r.missing_fields,
                issues=[ISSUE_MESSAGES.get(c, c) for c in r.missing_fields],
                admin_url=_admin_url(merchant.shop_domain, r.product_gid),
            )
            for r in rows
        ],
    )


# ----------------------------------------------------------------- meta-mapping rules


def _rule_out(rule: MetaMappingRule) -> MappingRuleOut:
    return MappingRuleOut(
        id=rule.id,
        source_type=rule.source_type.value,  # type: ignore[arg-type]
        source_key=rule.source_key,
        match_pattern=rule.match_pattern,
        target_property=rule.target_property,
        transform=rule.transform,
        priority=rule.priority,
        active=rule.active,
    )


@router.get("/mapping-rules", response_model=list[MappingRuleOut])
async def list_mapping_rules(
    merchant: CurrentMerchant, session: SessionDep
) -> list[MappingRuleOut]:
    """List the merchant's meta-mapping rules."""
    return [_rule_out(r) for r in await MappingRuleRepository(session).list_for(merchant.id)]


# Products a rule preview reads from Shopify: four pages of MAPPING_PREVIEW_QUERY.
PREVIEW_PRODUCTS = 100


def _validate_rule(body: MappingRuleIn) -> None:
    try:
        re.compile(body.match_pattern)
    except re.error as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid regex: {exc}") from exc
    if body.source_type == "metafield" and "." not in body.source_key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "metafield rules need source_key 'namespace.key'"
        )


async def _rules_changed(merchant: CurrentMerchant, session: SessionDep, ttl: int) -> None:
    """The llm.txt feed applies the rules, so a cached copy is stale after any change."""
    await LlmTxtCacheRepository(session, ttl).invalidate(merchant.shop_domain)


@router.post(
    "/mapping-rules",
    response_model=MappingRuleOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=write,
)
async def create_mapping_rule(
    body: MappingRuleIn, merchant: CurrentMerchant, session: SessionDep, settings: SettingsDep
) -> MappingRuleOut:
    """Create a meta-mapping rule and invalidate the cached `llm.txt`."""
    _validate_rule(body)
    rule = MetaMappingRule(
        merchant_id=merchant.id,
        source_type=MappingSource(body.source_type),
        source_key=body.source_key,
        match_pattern=body.match_pattern,
        target_property=body.target_property,
        transform=body.transform,
        priority=body.priority,
        active=body.active,
    )
    out = _rule_out(await MappingRuleRepository(session).add(rule))
    await _rules_changed(merchant, session, settings.llm_txt_cache_seconds)
    return out


@router.post("/mapping-rules/preview", response_model=MappingPreviewOut)
async def preview_mapping_rule(
    body: MappingRuleIn,
    merchant: CurrentMerchant,
    session: SessionDep,
    settings: SettingsDep,
    http: HttpClientDep,
    tokens: TokenProviderDep,
) -> MappingPreviewOut:
    """What the rule would extract from the store's first active products, read live from
    Shopify. Saves nothing."""
    _validate_rule(body)
    rule = MappingRule(
        source_type=body.source_type,
        source_key=body.source_key,
        match_pattern=body.match_pattern,
        target_property=body.target_property,
        transform=body.transform,
        priority=body.priority,
    )
    client = shopify_client_for(merchant, http, tokens, settings)
    products: list[dict[str, Any]] = []
    try:
        async for product in client.paginate(MAPPING_PREVIEW_QUERY, ["products"]):
            products.append(product)
            if len(products) >= PREVIEW_PRODUCTS:
                break
    except ShopifyAuthError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Shopify rejected the app's access token. Reinstall the app.",
        ) from exc
    except ShopifyError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Shopify request failed: {exc}") from exc
    others = await MappingRuleRepository(session).domain_rules(merchant.id)
    return MappingPreviewOut.model_validate(preview_rule(rule, products, others))


@router.patch("/mapping-rules/{rule_id}", response_model=MappingRuleOut, dependencies=write)
async def update_mapping_rule(
    rule_id: uuid.UUID,
    body: MappingRulePatch,
    merchant: CurrentMerchant,
    session: SessionDep,
    settings: SettingsDep,
) -> MappingRuleOut:
    """Turn a meta-mapping rule on or off and invalidate the cached `llm.txt`."""
    repo = MappingRuleRepository(session)
    rule = await repo.get(merchant.id, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    rule.active = body.active
    out = _rule_out(await repo.add(rule))
    await _rules_changed(merchant, session, settings.llm_txt_cache_seconds)
    return out


@router.delete(
    "/mapping-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=write
)
async def delete_mapping_rule(
    rule_id: uuid.UUID, merchant: CurrentMerchant, session: SessionDep, settings: SettingsDep
) -> None:
    """Delete a meta-mapping rule and invalidate the cached `llm.txt`."""
    if not await MappingRuleRepository(session).delete(merchant.id, rule_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    await _rules_changed(merchant, session, settings.llm_txt_cache_seconds)


# ----------------------------------------------------------------- brand FAQs (llm.txt)


@router.get("/faqs", response_model=FaqList)
async def get_faqs(merchant: CurrentMerchant) -> FaqList:
    """Return the store FAQs published in `llm.txt`."""
    return FaqList.model_validate({"faqs": merchant.settings.get("faqs", [])})


@router.put("/faqs", response_model=FaqList, dependencies=write)
async def put_faqs(
    body: FaqList, merchant: CurrentMerchant, session: SessionDep, settings: SettingsDep
) -> FaqList:
    """Replace the store FAQs and invalidate the cached `llm.txt`."""
    merchant.settings = {**merchant.settings, "faqs": [f.model_dump() for f in body.faqs]}
    flag_modified(merchant, "settings")
    session.add(merchant)
    await session.commit()
    await LlmTxtCacheRepository(session, settings.llm_txt_cache_seconds).invalidate(
        merchant.shop_domain
    )
    return body
