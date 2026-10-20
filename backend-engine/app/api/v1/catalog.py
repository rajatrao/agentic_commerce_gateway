"""Catalog intelligence routes: readiness, findings, enrichment, review, metafield write-back.

All routes act on the authenticated merchant only (tenant scoping comes from the service JWT's
shop claim); writes require the `merchant:write` scope.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import (
    CurrentMerchant,
    HttpClientDep,
    SessionDep,
    SettingsDep,
    TokenProviderDep,
    require_service_scopes,
)
from app.api.v1.audits import _admin_url
from app.jobs.audit_worker import QueueWorker
from app.jobs.catalog_audit import shopify_client_for
from app.models import (
    AttributeSuggestion,
    AuditFinding,
    AuditRun,
    CatalogChange,
    CatalogJob,
    CatalogJobKind,
    CatalogProduct,
    EnrichmentJob,
    Merchant,
    SuggestionStatus,
)
from app.repositories.catalog import CatalogRepository
from app.repositories.catalog_config import (
    CatalogConfig,
    CatalogConfigRepository,
    catalog_settings,
)
from app.repositories.catalog_jobs import CatalogJobRepository
from app.repositories.enrichment import EnrichmentRepository
from app.schemas.catalog import (
    AttributeRow,
    AutoApprovalReport,
    AutoApprovalSchemaOut,
    CatalogJobOut,
    CatalogProductOut,
    CatalogProductPage,
    CatalogSettingsIn,
    CatalogSettingsOut,
    ChangeOut,
    ChangePage,
    EnrichmentRequest,
    FindingOut,
    FindingPage,
    JobOut,
    MappingIn,
    MappingOut,
    OverviewOut,
    ProductDetailOut,
    PublishRequest,
    ReviewRequest,
    RunPoint,
    SchemaOut,
    SuggestionOut,
    SuggestionPage,
)
from app.services import catalog_publisher
from app.services.catalog_schema import (
    BUILTIN_SCHEMAS,
    AutoApproval,
    CategorySchema,
    display_value,
)
from app.services.metafield_writer import COMPATIBLE_TYPES, MetafieldWriter
from app.services.product_sync import queue_refresh
from app.services.readiness import DEFAULT_WEIGHTS
from app.services.shopify_client import ShopifyAuthError, ShopifyError
from app.services.taxonomy_writer import TaxonomyWriter

router = APIRouter(prefix="/merchant/catalog", tags=["catalog-intelligence"])
write = [Depends(require_service_scopes("merchant:write"))]
BUILTIN_KEYS = {s["key"] for s in BUILTIN_SCHEMAS}


# ----------------------------------------------------------------- serializers


# One severity or a comma-separated list, e.g. "critical,high".
SEVERITY_LIST = "^(critical|high|medium|low)(,(critical|high|medium|low))*$"


def _run(run: AuditRun) -> RunPoint:
    return RunPoint(
        id=run.id,
        status=run.status.value,
        started_at=run.started_at,
        finished_at=run.finished_at,
        products_scanned=run.products_scanned,
        avg_score=run.avg_score,
        avg_readiness=run.avg_readiness,
        last_error=run.last_error,
    )


def _job(job: EnrichmentJob, with_results: bool = False) -> JobOut:
    return JobOut(
        id=job.id,
        status=job.status.value,
        use_ai=job.use_ai,
        total=job.total,
        succeeded=job.succeeded,
        failed=job.failed,
        created_at=job.created_at,
        finished_at=job.finished_at,
        last_error=job.last_error,
        results=job.results
        if with_results
        else {g: r for g, r in job.results.items() if r.get("status") == "error"},
        product_gids=list(job.product_gids) if with_results else None,
    )


def _catalog_job(job: CatalogJob) -> CatalogJobOut:
    return CatalogJobOut(
        id=job.id,
        kind=job.kind.value,
        status=job.status.value,
        source=job.source,
        total=job.total,
        processed=job.processed,
        succeeded=job.succeeded,
        failed=job.failed,
        created_at=job.created_at,
        finished_at=job.finished_at,
        last_error=job.last_error,
        results=job.results,
    )


def _wake_catalog_worker(request: Request) -> None:
    worker: QueueWorker | None = getattr(request.app.state, "catalog_worker", None)
    if worker is not None:
        worker.wake()


def _change(c: CatalogChange) -> ChangeOut:
    assert c.id is not None
    return ChangeOut(
        id=c.id,
        product_gid=c.product_gid,
        product_title=c.product_title,
        suggestion_id=c.suggestion_id,
        attribute_key=c.attribute_key,
        action=c.action,
        before=c.before,
        after=c.after,
        details=c.details,
        created_at=c.created_at,
        reverted_at=c.reverted_at,
        revertible=c.action == "publish" and c.reverted_at is None,
    )


def _product(
    merchant: Merchant, p: CatalogProduct, findings: dict[str, int] | None = None
) -> CatalogProductOut:
    return CatalogProductOut(
        product_gid=p.product_gid,
        title=p.title,
        status=p.status,
        product_type=p.product_type,
        vendor=p.vendor,
        category_name=p.category_name,
        schema_key=p.schema_key,
        readiness_score=p.readiness_score,
        discovery_score=p.discovery_score,
        findings=findings or {},
        admin_url=_admin_url(merchant.shop_domain, p.product_gid),
        synced_at=p.synced_at,
    )


def _finding(f: AuditFinding) -> FindingOut:
    assert f.id is not None
    return FindingOut(
        id=f.id,
        product_gid=f.product_gid,
        product_title=f.product_title,
        code=f.code,
        attribute_key=f.attribute_key,
        severity=f.severity.value,
        method=f.method,
        message=f.message,
        why_it_matters=f.why_it_matters,
        recommended_action=f.recommended_action,
        evidence=f.evidence,
        confidence=f.confidence,
        created_at=f.created_at,
    )


def _suggestion(config: CatalogConfig, s: AttributeSuggestion) -> SuggestionOut:
    attr = catalog_publisher.attribute_for(config, s)
    return SuggestionOut(
        id=s.id,
        product_gid=s.product_gid,
        product_title=s.product_title,
        schema_key=s.schema_key,
        attribute_key=s.attribute_key,
        attribute_label=attr.label if attr else s.attribute_key,
        data_type=s.data_type,
        unit=attr.unit if attr else None,
        allowed_values=attr.allowed_values if attr else None,
        current_value=s.current_value,
        proposed_value=s.proposed_value,
        approved_value=s.approved_value,
        display_current=display_value(s.current_value, attr)
        if s.current_value is not None
        else "Not set",
        display_proposed=display_value(
            s.approved_value if s.approved_value is not None else s.proposed_value, attr
        ),
        evidence=s.evidence,
        evidence_source=s.evidence_source,
        confidence=s.confidence,
        method=s.method,
        explanation=s.explanation,
        validation_status=s.validation_status,
        validation_errors=s.validation_errors,
        status=s.status.value,
        last_error=s.last_error,
        created_at=s.created_at,
        reviewed_at=s.reviewed_at,
        published_at=s.published_at,
    )


def _gid(product_id: str) -> str:
    if product_id.startswith("gid://shopify/Product/"):
        return product_id
    if not product_id.isdigit():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid product id")
    return f"gid://shopify/Product/{product_id}"


def _writer(merchant: Merchant, http: Any, tokens: Any, settings: Any) -> MetafieldWriter:
    return MetafieldWriter(shopify_client_for(merchant, http, tokens, settings))


def _shopify_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ShopifyAuthError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "Shopify rejected the app's access token. Reinstall the app or grant write_products.",
        )
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"Shopify request failed: {exc}")


# ----------------------------------------------------------------- dashboard


@router.get("/overview", response_model=OverviewOut)
async def overview(merchant: CurrentMerchant, session: SessionDep, request: Request) -> OverviewOut:
    catalog = CatalogRepository(session)
    enrichment = EnrichmentRepository(session)
    run = await catalog.latest_completed_run(merchant.id)
    summary = await catalog.finding_summary(merchant.id, run.id if run else None)
    settings = catalog_settings(merchant)
    stats = run.stats if run else {}
    changes, _ = await catalog.changes(merchant.id, limit=5)
    jobs = CatalogJobRepository(session)
    discovery = await jobs.active(merchant.id, CatalogJobKind.DISCOVERY) or (
        await jobs.latest_completed(merchant.id, CatalogJobKind.DISCOVERY)
    )
    publishing = await jobs.active(merchant.id, CatalogJobKind.PUBLISH)
    return OverviewOut(
        has_audit=run is not None,
        products_analyzed=run.products_scanned if run else 0,
        readiness_score=run.avg_readiness if run else None,
        discovery_score=run.avg_score if run else None,
        ready_products=int(stats.get("ready_products", 0)),
        ready_threshold=settings["ready_threshold"],
        products_missing_required=summary["products_missing_required"],
        missing_attribute_instances=summary["missing_attribute_instances"],
        findings_by_severity=summary["by_severity"],
        top_findings=summary["by_code"],
        suggestions=await enrichment.status_counts(merchant.id),
        failed_validations=await enrichment.invalid_open_count(merchant.id),
        ai_enabled=getattr(request.app.state, "enricher", None) is not None,
        trend=[_run(r) for r in await catalog.trend(merchant.id)],
        recent_runs=[_run(r) for r in await catalog.recent_runs(merchant.id, 5)],
        recent_jobs=[_job(j) for j in await enrichment.list_jobs(merchant.id, 5)],
        recent_changes=[_change(c) for c in changes],
        discovery_tests=list(stats.get("discovery_tests", [])),
        discovery_run=_catalog_job(discovery) if discovery else None,
        publish_job=_catalog_job(publishing) if publishing else None,
    )


# ----------------------------------------------------------------- products and findings


@router.get("/runs", response_model=list[RunPoint])
async def list_runs(
    merchant: CurrentMerchant, session: SessionDep, limit: int = Query(20, ge=1, le=100)
) -> list[RunPoint]:
    """Catalog audit (sync) history, newest first."""
    return [_run(r) for r in await CatalogRepository(session).recent_runs(merchant.id, limit)]


@router.get("/products", response_model=CatalogProductPage)
async def list_products(
    merchant: CurrentMerchant,
    session: SessionDep,
    schema: str | None = Query(None, max_length=64),
    max_readiness: int = Query(100, ge=0, le=100),
    severity: str | None = Query(None, pattern=SEVERITY_LIST),
    code: str | None = Query(None, max_length=64),
    attribute: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
) -> CatalogProductPage:
    catalog = CatalogRepository(session)
    run = await catalog.latest_completed_run(merchant.id)
    rows, total, counts = await catalog.products(
        merchant.id,
        run.id if run else None,
        schema_key=schema,
        max_readiness=max_readiness,
        severity=severity,
        code=code,
        attribute_key=attribute,
        search=q,
        limit=limit,
        offset=offset,
    )
    return CatalogProductPage(
        total=total, items=[_product(merchant, r, counts.get(r.product_gid)) for r in rows]
    )


@router.get("/products/{product_id}", response_model=ProductDetailOut)
async def product_detail(
    product_id: str, merchant: CurrentMerchant, session: SessionDep
) -> ProductDetailOut:
    gid = _gid(product_id)
    catalog = CatalogRepository(session)
    product = await catalog.product(merchant.id, gid)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "product not found; run a catalog audit")
    run = await catalog.latest_completed_run(merchant.id)
    config = await CatalogConfigRepository(session).load(merchant)
    findings, _ = await catalog.findings(
        merchant.id, run.id if run else None, product_gid=gid, limit=250
    )
    suggestions, _ = await EnrichmentRepository(session).suggestions(
        merchant.id,
        product_gid=gid,
        statuses=[
            SuggestionStatus.PENDING,
            SuggestionStatus.APPROVED,
            SuggestionStatus.FAILED,
            SuggestionStatus.PUBLISHED,
        ],
        limit=250,
    )
    changes, _ = await catalog.changes(merchant.id, product_gid=gid, limit=50)
    schema = config.schemas.get(product.schema_key or "")
    audit = await catalog.product_audit(run.id, gid) if run else None
    breakdown = audit.readiness_breakdown if audit else {}
    rows: list[AttributeRow] = []
    for attr in schema.attributes if schema else []:
        cur = product.attributes.get(attr.key) or {}
        mapping = config.mappings.get(attr.key)
        rows.append(
            AttributeRow(
                key=attr.key,
                label=attr.label,
                type=attr.type,
                unit=attr.unit,
                importance=attr.importance,
                required=attr.required,
                current=cur.get("value"),
                current_display=display_value(cur.get("value"), attr)
                if cur.get("value") is not None
                else ("Invalid" if cur.get("errors") else "Not set"),
                source=cur.get("source"),
                errors=cur.get("errors") or [],
                metafield=f"{mapping.namespace}.{mapping.key}" if mapping else None,
            )
        )
    counts = await catalog.severity_counts_by_product(merchant.id, run.id if run else None, [gid])
    return ProductDetailOut(
        product=_product(merchant, product, counts.get(gid)),
        schema_name=schema.name if schema else None,
        readiness_breakdown=breakdown,
        attributes=rows,
        findings=[_finding(f) for f in findings],
        suggestions=[_suggestion(config, s) for s in suggestions],
        changes=[_change(c) for c in changes],
    )


@router.get("/findings", response_model=FindingPage)
async def list_findings(
    merchant: CurrentMerchant,
    session: SessionDep,
    severity: str | None = Query(None, pattern=SEVERITY_LIST),
    code: str | None = Query(None, max_length=64),
    method: str | None = Query(None, pattern="^(rule|ai)$"),
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
) -> FindingPage:
    catalog = CatalogRepository(session)
    run = await catalog.latest_completed_run(merchant.id)
    rows, total = await catalog.findings(
        merchant.id,
        run.id if run else None,
        severity=severity,
        code=code,
        method=method,
        limit=limit,
        offset=offset,
    )
    return FindingPage(total=total, items=[_finding(f) for f in rows])


# ----------------------------------------------------------------- enrichment


@router.post(
    "/enrichment-jobs",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=write,
)
async def start_enrichment(
    body: EnrichmentRequest,
    merchant: CurrentMerchant,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> JobOut:
    catalog = CatalogRepository(session)
    limit = settings.enrichment_max_products
    if body.lowest_readiness:
        gids = await catalog.lowest_readiness_gids(merchant.id, min(body.lowest_readiness, limit))
    else:
        requested = list(dict.fromkeys(_gid(g) for g in body.product_gids))
        if len(requested) > limit:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"at most {limit} products per job"
            )
        gids = await catalog.existing_gids(merchant.id, requested)
    if not gids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "no products to enrich; run a catalog audit first so products are imported",
        )
    repo = EnrichmentRepository(session)
    if await repo.active_job_for(merchant.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "an enrichment job is already running")
    ai_available = getattr(request.app.state, "enricher", None) is not None
    job = await repo.create_job(merchant.id, gids, use_ai=body.use_ai and ai_available)
    worker: QueueWorker | None = getattr(request.app.state, "enrichment_worker", None)
    if worker is not None:
        worker.wake()
    return _job(job)


@router.get("/enrichment-jobs", response_model=list[JobOut])
async def list_enrichment_jobs(merchant: CurrentMerchant, session: SessionDep) -> list[JobOut]:
    return [_job(j) for j in await EnrichmentRepository(session).list_jobs(merchant.id)]


@router.get("/enrichment-jobs/{job_id}", response_model=JobOut)
async def get_enrichment_job(
    job_id: uuid.UUID, merchant: CurrentMerchant, session: SessionDep
) -> JobOut:
    job = await EnrichmentRepository(session).get_job(merchant.id, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return _job(job, with_results=True)


@router.post(
    "/enrichment-jobs/{job_id}/retry",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=write,
)
async def retry_enrichment_job(
    job_id: uuid.UUID, merchant: CurrentMerchant, session: SessionDep, request: Request
) -> JobOut:
    """Queue a new job for the products that failed in this one."""
    repo = EnrichmentRepository(session)
    job = await repo.get_job(merchant.id, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    failed = [g for g, r in job.results.items() if r.get("status") == "error"]
    if job.status.value == "failed":
        failed += [g for g in job.product_gids if g not in job.results]
    if not failed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no failed products to retry")
    if await repo.active_job_for(merchant.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "an enrichment job is already running")
    retry = await repo.create_job(merchant.id, failed, use_ai=job.use_ai)
    worker: QueueWorker | None = getattr(request.app.state, "enrichment_worker", None)
    if worker is not None:
        worker.wake()
    return _job(retry)


# ----------------------------------------------------------------- review


@router.get("/suggestions", response_model=SuggestionPage)
async def list_suggestions(
    merchant: CurrentMerchant,
    session: SessionDep,
    status_: str = Query("pending", alias="status", max_length=100),
    product: str | None = Query(None, max_length=100),
    attribute: str | None = Query(None, max_length=64),
    validation: str | None = Query(None, pattern="^(valid|warning|invalid|unknown)$"),
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
) -> SuggestionPage:
    try:
        statuses = [SuggestionStatus(s) for s in status_.split(",") if s]
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown status") from exc
    config = await CatalogConfigRepository(session).load(merchant)
    rows, total = await EnrichmentRepository(session).suggestions(
        merchant.id,
        statuses=statuses,
        product_gid=_gid(product) if product else None,
        attribute_key=attribute,
        validation_status=validation,
        limit=limit,
        offset=offset,
    )
    return SuggestionPage(total=total, items=[_suggestion(config, s) for s in rows])


@router.post("/suggestions/review", dependencies=write)
async def review_suggestions(
    body: ReviewRequest, merchant: CurrentMerchant, session: SessionDep
) -> dict[str, Any]:
    config = await CatalogConfigRepository(session).load(merchant)
    results = await catalog_publisher.review(
        session,
        merchant,
        config,
        [catalog_publisher.ReviewAction(a.id, a.action, a.value) for a in body.actions],
    )
    return {
        "results": results,
        "succeeded": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
    }


# ----------------------------------------------------------------- write-back


@router.post("/publish/preview", dependencies=write)
async def preview_publish(
    body: PublishRequest,
    merchant: CurrentMerchant,
    session: SessionDep,
    settings: SettingsDep,
    http: HttpClientDep,
    tokens: TokenProviderDep,
) -> dict[str, Any]:
    """What publishing would write, read live from Shopify. Writes nothing."""
    config = await CatalogConfigRepository(session).load(merchant)
    client = shopify_client_for(merchant, http, tokens, settings)
    try:
        items, category = await catalog_publisher.preview(
            session,
            merchant,
            config,
            MetafieldWriter(client),
            body.suggestion_ids,
            settings.publish_preview_max,
            TaxonomyWriter(client),
        )
    except ShopifyError as exc:
        raise _shopify_error(exc) from exc
    return {
        "items": items,
        "category_metafields": category,
        "writable": sum(1 for i in items if i["action"] in ("create", "update")),
        "unchanged": sum(1 for i in items if i["action"] == "unchanged"),
        "blocked": sum(1 for i in items if i["action"] == "blocked"),
    }


@router.post(
    "/publish",
    response_model=CatalogJobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=write,
)
async def publish(
    body: PublishRequest,
    merchant: CurrentMerchant,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> CatalogJobOut:
    """Queue a background job that writes approved suggestions to Shopify metafields.
    Requires `confirm: true`. Poll `/publish-jobs/{id}` for progress."""
    if not body.confirm:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "publishing changes live Shopify data; preview first and send confirm=true",
        )
    jobs = CatalogJobRepository(session)
    if await jobs.active(merchant.id, CatalogJobKind.PUBLISH) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a publish job is already running")
    rows, _ = await EnrichmentRepository(session).suggestions(
        merchant.id,
        statuses=[SuggestionStatus.APPROVED],
        ids=body.suggestion_ids,
        limit=settings.publish_max_suggestions,
    )
    if not rows:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no approved values to publish")
    job, _ = await jobs.create(
        merchant.id,
        CatalogJobKind.PUBLISH,
        params={"suggestion_ids": [str(r.id) for r in rows]},
        total=len(rows),
    )
    _wake_catalog_worker(request)
    return _catalog_job(job)


@router.get("/publish-jobs", response_model=list[CatalogJobOut])
async def list_publish_jobs(
    merchant: CurrentMerchant, session: SessionDep, limit: int = Query(20, ge=1, le=100)
) -> list[CatalogJobOut]:
    rows = await CatalogJobRepository(session).recent(merchant.id, CatalogJobKind.PUBLISH, limit)
    return [_catalog_job(j) for j in rows]


@router.get("/publish-jobs/{job_id}", response_model=CatalogJobOut)
async def get_publish_job(
    job_id: uuid.UUID, merchant: CurrentMerchant, session: SessionDep
) -> CatalogJobOut:
    job = await CatalogJobRepository(session).get(merchant.id, job_id)
    if job is None or job.kind is not CatalogJobKind.PUBLISH:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return _catalog_job(job)


# ----------------------------------------------------------------- discovery search tests


@router.post(
    "/discovery-runs",
    response_model=CatalogJobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=write,
)
async def start_discovery_run(
    merchant: CurrentMerchant, session: SessionDep, request: Request
) -> CatalogJobOut:
    """Ask the store's search each schema's shopper questions and measure what comes back."""
    if await CatalogRepository(session).active_product_count(merchant.id) == 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "run a catalog audit first so products are known"
        )
    jobs = CatalogJobRepository(session)
    if await jobs.active(merchant.id, CatalogJobKind.DISCOVERY) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a discovery test is already running")
    job, _ = await jobs.create(merchant.id, CatalogJobKind.DISCOVERY)
    _wake_catalog_worker(request)
    return _catalog_job(job)


@router.get("/discovery-runs", response_model=list[CatalogJobOut])
async def list_discovery_runs(
    merchant: CurrentMerchant, session: SessionDep, limit: int = Query(10, ge=1, le=50)
) -> list[CatalogJobOut]:
    rows = await CatalogJobRepository(session).recent(merchant.id, CatalogJobKind.DISCOVERY, limit)
    return [_catalog_job(j) for j in rows]


@router.get("/discovery-runs/{job_id}", response_model=CatalogJobOut)
async def get_discovery_run(
    job_id: uuid.UUID, merchant: CurrentMerchant, session: SessionDep
) -> CatalogJobOut:
    job = await CatalogJobRepository(session).get(merchant.id, job_id)
    if job is None or job.kind is not CatalogJobKind.DISCOVERY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return _catalog_job(job)


@router.get("/changes", response_model=ChangePage)
async def list_changes(
    merchant: CurrentMerchant,
    session: SessionDep,
    product: str | None = Query(None, max_length=100),
    writes_only: bool = False,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
) -> ChangePage:
    rows, total = await CatalogRepository(session).changes(
        merchant.id,
        product_gid=_gid(product) if product else None,
        actions=["publish", "publish_failed", "revert"] if writes_only else None,
        limit=limit,
        offset=offset,
    )
    return ChangePage(total=total, items=[_change(c) for c in rows])


@router.post("/changes/{change_id}/revert", response_model=ChangeOut, dependencies=write)
async def revert_change(
    change_id: int,
    merchant: CurrentMerchant,
    session: SessionDep,
    settings: SettingsDep,
    http: HttpClientDep,
    tokens: TokenProviderDep,
    request: Request,
) -> ChangeOut:
    try:
        row = await catalog_publisher.revert(
            session, merchant, _writer(merchant, http, tokens, settings), change_id
        )
    except catalog_publisher.ReviewError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ShopifyError as exc:
        await session.rollback()
        raise _shopify_error(exc) from exc
    await queue_refresh(session, merchant.id, row.product_gid, "revert")
    _wake_catalog_worker(request)
    return _change(row)


# ----------------------------------------------------------------- configuration


@router.get("/schemas", response_model=list[SchemaOut])
async def list_schemas(merchant: CurrentMerchant, session: SessionDep) -> list[SchemaOut]:
    repo = CatalogConfigRepository(session)
    customized = {o.key for o in await repo.overrides(merchant.id)}
    return [
        SchemaOut(key=k, customized=k in customized, builtin=k in BUILTIN_KEYS, definition=s)
        for k, s in (await repo.schemas(merchant.id)).items()
    ]


@router.put("/schemas/{key}", response_model=SchemaOut, dependencies=write)
async def put_schema(
    key: str, body: CategorySchema, merchant: CurrentMerchant, session: SessionDep
) -> SchemaOut:
    if body.key != key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "key does not match the URL")
    await CatalogConfigRepository(session).save_schema(merchant.id, body)
    return SchemaOut(key=key, customized=True, builtin=key in BUILTIN_KEYS, definition=body)


@router.delete("/schemas/{key}", status_code=status.HTTP_204_NO_CONTENT, dependencies=write)
async def reset_schema(key: str, merchant: CurrentMerchant, session: SessionDep) -> None:
    """Drop the merchant's version: a built-in schema reverts to its default, an added one
    is removed."""
    if not await CatalogConfigRepository(session).reset_schema(merchant.id, key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no customized schema with that key")


# ----------------------------------------------------------------- auto-approval


@router.get("/auto-approval", response_model=AutoApprovalReport)
async def auto_approval_report(
    merchant: CurrentMerchant, session: SessionDep
) -> AutoApprovalReport:
    """Each schema's rule, what it would approve among pending suggestions now (and what
    blocks the rest), and the values approved automatically so far."""
    config = await CatalogConfigRepository(session).load(merchant)
    preview = await catalog_publisher.auto_approval_preview(session, merchant, config)
    catalog = CatalogRepository(session)
    totals = await catalog.change_counts_by_schema(merchant.id, "auto_approve")
    recent, _ = await catalog.changes(merchant.id, actions=["auto_approve"], limit=50)
    ids = [c.suggestion_id for c in recent if c.suggestion_id]
    current, _ = await EnrichmentRepository(session).suggestions(
        merchant.id, ids=ids, limit=len(ids) or 1
    )
    return AutoApprovalReport(
        schemas=[
            AutoApprovalSchemaOut(
                schema_key=key,
                schema_name=schema.name,
                rule=schema.auto_approve,
                attributes=[a.key for a in schema.attributes],
                pending=preview.get(key, {}).get("pending", 0),
                qualifying=preview.get(key, {}).get("qualifying", 0),
                blocked_by=preview.get(key, {}).get("blocked_by", {}),
                auto_approved_total=totals.get(key, 0),
            )
            for key, schema in config.schemas.items()
        ],
        recent=[_change(c) for c in recent],
        statuses={str(s.id): s.status.value for s in current},
    )


@router.put("/schemas/{key}/auto-approval", response_model=SchemaOut, dependencies=write)
async def put_auto_approval(
    key: str, body: AutoApproval, merchant: CurrentMerchant, session: SessionDep
) -> SchemaOut:
    """Turn auto-approval on or off for one schema, or change its threshold and attributes."""
    repo = CatalogConfigRepository(session)
    schema = (await repo.schemas(merchant.id)).get(key)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no schema with that key")
    try:
        updated = CategorySchema.model_validate(
            schema.model_dump(mode="json") | {"auto_approve": body.model_dump(mode="json")}
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await repo.save_schema(merchant.id, updated)
    return SchemaOut(key=key, customized=True, builtin=key in BUILTIN_KEYS, definition=updated)


@router.post("/auto-approval/{key}/apply", dependencies=write)
async def apply_auto_approval(
    key: str, merchant: CurrentMerchant, session: SessionDep
) -> dict[str, int]:
    """Apply an enabled rule to the schema's pending suggestions now."""
    config = await CatalogConfigRepository(session).load(merchant)
    try:
        approved = await catalog_publisher.auto_approve_pending(session, merchant, config, key)
    except catalog_publisher.ReviewError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"approved": approved}


def _attribute_index(config: CatalogConfig) -> dict[str, tuple[str, list[str]]]:
    index: dict[str, tuple[str, list[str]]] = {}
    for schema in config.schemas.values():
        for attr in schema.attributes:
            type_, labels = index.get(attr.key, (attr.type, []))
            index[attr.key] = (type_, [*labels, f"{schema.name}: {attr.label}"])
    return index


@router.get("/mappings", response_model=list[MappingOut])
async def list_mappings(
    merchant: CurrentMerchant,
    session: SessionDep,
    settings: SettingsDep,
    http: HttpClientDep,
    tokens: TokenProviderDep,
    check_shopify: bool = False,
) -> list[MappingOut]:
    config = await CatalogConfigRepository(session).load(merchant)
    definitions: dict[tuple[str, str], str] | None = None
    if check_shopify:
        try:
            definitions = await _writer(merchant, http, tokens, settings).definitions()
        except ShopifyError as exc:
            raise _shopify_error(exc) from exc
    out = []
    for key, (attr_type, labels) in sorted(_attribute_index(config).items()):
        m = config.mappings.get(key)
        if m is None:
            continue
        item = MappingOut(
            attribute_key=key,
            labels=labels,
            attribute_type=attr_type,
            namespace=m.namespace,
            key=m.key,
            type=m.type,
            enabled=m.enabled,
            source=m.source,
            compatible_types=sorted(COMPATIBLE_TYPES[attr_type]),
        )
        if definitions is not None:
            existing = definitions.get((m.namespace, m.key))
            item.shopify_definition = existing
            item.status = (
                "will_create" if existing is None else "ok" if existing == m.type else "conflict"
            )
        out.append(item)
    return out


@router.put("/mappings/{attribute_key}", response_model=MappingOut, dependencies=write)
async def put_mapping(
    attribute_key: str, body: MappingIn, merchant: CurrentMerchant, session: SessionDep
) -> MappingOut:
    repo = CatalogConfigRepository(session)
    config = await repo.load(merchant)
    index = _attribute_index(config)
    if attribute_key not in index:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no schema defines this attribute")
    attr_type, labels = index[attribute_key]
    if body.type not in COMPATIBLE_TYPES[attr_type]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"a {attr_type} attribute can be stored as: {sorted(COMPATIBLE_TYPES[attr_type])}",
        )
    clash = next(
        (
            k
            for k, m in config.mappings.items()
            if k != attribute_key and (m.namespace, m.key) == (body.namespace, body.key)
        ),
        None,
    )
    if clash:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{body.namespace}.{body.key} is already mapped to {clash}"
        )
    await repo.save_mapping(
        merchant.id, attribute_key, body.namespace, body.key, body.type, body.enabled
    )
    return MappingOut(
        attribute_key=attribute_key,
        labels=labels,
        attribute_type=attr_type,
        namespace=body.namespace,
        key=body.key,
        type=body.type,
        enabled=body.enabled,
        source="custom",
        compatible_types=sorted(COMPATIBLE_TYPES[attr_type]),
    )


def _settings_out(merchant: Merchant, request: Request) -> CatalogSettingsOut:
    values = catalog_settings(merchant)
    enricher = getattr(request.app.state, "enricher", None)
    return CatalogSettingsOut(
        readiness_weights=values["readiness_weights"],
        default_weights=DEFAULT_WEIGHTS,
        ready_threshold=values["ready_threshold"],
        ai_enabled=enricher is not None,
        ai_provider=getattr(enricher, "provider", None),
        ai_model=getattr(enricher, "model", None),
        auto_publish=False,
        auto_publish_note=(
            "Nothing is written to Shopify without your confirmation. Each category schema can "
            "opt in to auto-approval of high-confidence values that rule-based extraction "
            "confirms; those still go through the write preview, and the auto-approval report "
            "lists every one."
        ),
    )


@router.get("/settings", response_model=CatalogSettingsOut)
async def get_catalog_settings(merchant: CurrentMerchant, request: Request) -> CatalogSettingsOut:
    return _settings_out(merchant, request)


@router.put("/settings", response_model=CatalogSettingsOut, dependencies=write)
async def put_catalog_settings(
    body: CatalogSettingsIn, merchant: CurrentMerchant, session: SessionDep, request: Request
) -> CatalogSettingsOut:
    unknown = set(body.readiness_weights) - set(DEFAULT_WEIGHTS)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown components: {sorted(unknown)}"
        )
    if any(not 0 <= v <= 100 for v in body.readiness_weights.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "weights must be 0-100")
    weights = {**DEFAULT_WEIGHTS, **body.readiness_weights}
    if sum(weights.values()) == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "weights cannot all be zero")
    await CatalogConfigRepository(session).save_settings(
        merchant, {"readiness_weights": weights, "ready_threshold": body.ready_threshold}
    )
    return _settings_out(merchant, request)
