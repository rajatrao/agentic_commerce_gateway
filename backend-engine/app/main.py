"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.agent import protocol_error_response
from app.api.mcp import mount_mcp
from app.api.v1.router import api_router, internal_router, public_router
from app.core.config import get_settings
from app.core.db import create_database
from app.core.logging import configure_logging
from app.jobs.audit_worker import AuditWorker
from app.jobs.catalog_jobs import CatalogJobWorker
from app.jobs.enrichment_worker import EnrichmentWorker
from app.models import CatalogJobKind
from app.protocols.acp_adapter import AcpAdapter
from app.protocols.base import ProtocolError
from app.protocols.ucp_adapter import UcpAdapter
from app.services.ai_enricher import enricher_from_settings
from app.services.shopify_client import ShopifyTokenProvider

ACP, UCP = AcpAdapter(), UcpAdapter()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.db = create_database(settings)
    app.state.http_client = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        limits=httpx.Limits(max_connections=50),
    )
    app.state.shopify_tokens = ShopifyTokenProvider(app.state.http_client, settings)
    worker = AuditWorker(app.state.db, app.state.http_client, app.state.shopify_tokens, settings)
    if settings.audit_worker_enabled:
        app.state.audit_worker = worker
        worker.start()
    enricher = app.state.enricher = enricher_from_settings(settings)
    enrichment = EnrichmentWorker(
        app.state.db, app.state.http_client, app.state.shopify_tokens, settings, app.state.enricher
    )
    if settings.enrichment_worker_enabled:
        app.state.enrichment_worker = enrichment
        enrichment.start()
    # Refreshes get their own worker so a long publish or discovery run never delays them.
    catalog_workers = [
        CatalogJobWorker(
            app.state.db, app.state.http_client, app.state.shopify_tokens, settings, kinds
        )
        for kinds in ((CatalogJobKind.REFRESH,), (CatalogJobKind.PUBLISH, CatalogJobKind.DISCOVERY))
    ]
    app.state.catalog_worker = catalog_workers[1]
    if settings.catalog_worker_enabled:
        for catalog_worker in catalog_workers:
            catalog_worker.start()
    try:
        async with app.state.mcp.session_manager.run():
            yield
    finally:
        for catalog_worker in catalog_workers:
            await catalog_worker.stop()
        await enrichment.stop()
        await worker.stop()
        if enricher is not None:
            await enricher.aclose()
        await app.state.http_client.aclose()
        await app.state.db.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.is_production)
    app = FastAPI(
        title="Agentic Commerce Gateway: backend-engine",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["*"],
        )

    app.include_router(api_router)
    app.include_router(internal_router)
    app.include_router(public_router)
    app.state.mcp = mount_mcp(app.router.routes, app.state)

    @app.exception_handler(ProtocolError)
    async def protocol_error(request: Request, exc: ProtocolError) -> JSONResponse:
        ucp_paths = ("/api/v1/ucp/", "/.well-known/ucp")
        adapter = UCP if request.url.path.startswith(ucp_paths) else ACP
        return protocol_error_response(adapter, request, exc)

    @app.get("/health", tags=["ops"])
    async def health(request: Request, response: Response) -> dict[str, str]:
        try:
            async with request.app.state.db.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "degraded", "database": "unreachable"}
        return {"status": "ok", "database": "ok"}

    return app


app = create_app()
