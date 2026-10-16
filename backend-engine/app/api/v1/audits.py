"""Catalog audit routes (AI Discovery Score), called by frontend-ui for a merchant."""

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import (
    CurrentMerchant,
    SessionDep,
    SettingsDep,
    require_service_scopes,
)
from app.jobs.audit_worker import AuditWorker
from app.models import AuditRun, MappingSource, MetaMappingRule
from app.repositories.audits import AuditRepository
from app.repositories.mapping_rules import MappingRuleRepository
from app.repositories.shared_state import LlmTxtCacheRepository
from app.schemas.audit import (
    AuditRunOut,
    AuditSummaryOut,
    FaqList,
    IssueCount,
    MappingRuleIn,
    MappingRuleOut,
    ProductAuditOut,
    ProductAuditPage,
)
from app.services.catalog_auditor import ISSUE_MESSAGES

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
    return [_rule_out(r) for r in await MappingRuleRepository(session).list_for(merchant.id)]


@router.post(
    "/mapping-rules",
    response_model=MappingRuleOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=write,
)
async def create_mapping_rule(
    body: MappingRuleIn, merchant: CurrentMerchant, session: SessionDep
) -> MappingRuleOut:
    try:
        re.compile(body.match_pattern)
    except re.error as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid regex: {exc}") from exc
    if body.source_type == "metafield" and "." not in body.source_key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "metafield rules need source_key 'namespace.key'"
        )
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
    return _rule_out(await MappingRuleRepository(session).add(rule))


@router.delete(
    "/mapping-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=write
)
async def delete_mapping_rule(
    rule_id: uuid.UUID, merchant: CurrentMerchant, session: SessionDep
) -> None:
    if not await MappingRuleRepository(session).delete(merchant.id, rule_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")


# ----------------------------------------------------------------- brand FAQs (llm.txt)


@router.get("/faqs", response_model=FaqList)
async def get_faqs(merchant: CurrentMerchant) -> FaqList:
    return FaqList.model_validate({"faqs": merchant.settings.get("faqs", [])})


@router.put("/faqs", response_model=FaqList, dependencies=write)
async def put_faqs(
    body: FaqList, merchant: CurrentMerchant, session: SessionDep, settings: SettingsDep
) -> FaqList:
    merchant.settings = {**merchant.settings, "faqs": [f.model_dump() for f in body.faqs]}
    flag_modified(merchant, "settings")
    session.add(merchant)
    await session.commit()
    await LlmTxtCacheRepository(session, settings.llm_txt_cache_seconds).invalidate(
        merchant.shop_domain
    )
    return body
