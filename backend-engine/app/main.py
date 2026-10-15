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
from app.core.db import get_engine
from app.core.logging import configure_logging
from app.protocols.acp_adapter import AcpAdapter
from app.protocols.base import ProtocolError
from app.protocols.ucp_adapter import UcpAdapter
from app.services.llm_txt_cache import LlmTxtCache
from app.services.shopify_client import ShopifyTokenProvider

ACP, UCP = AcpAdapter(), UcpAdapter()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.http_client = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        limits=httpx.Limits(max_connections=50),
    )
    app.state.shopify_tokens = ShopifyTokenProvider(app.state.http_client, settings)
    app.state.llm_txt_cache = LlmTxtCache(settings.llm_txt_cache_seconds)
    try:
        async with app.state.mcp.session_manager.run():
            yield
    finally:
        await app.state.http_client.aclose()
        get_engine().dispose()


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
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    app.include_router(api_router)
    app.include_router(internal_router)
    app.include_router(public_router)
    app.state.mcp = mount_mcp(app.router.routes, app.state)

    @app.exception_handler(ProtocolError)
    async def protocol_error(request: Request, exc: ProtocolError) -> JSONResponse:
        adapter = ACP if request.url.path.startswith("/api/v1/acp/") else UCP
        return protocol_error_response(adapter, request, exc)

    @app.get("/health", tags=["ops"])
    def health(response: Response) -> dict[str, str]:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "degraded", "database": "unreachable"}
        return {"status": "ok", "database": "ok"}

    return app


app = create_app()
