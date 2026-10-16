"""Public, unauthenticated routes consumed by AI agents."""

import re
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.api.deps import HttpClientDep, SessionDep, SettingsDep, TokenProviderDep
from app.jobs.catalog_audit import shopify_client_for
from app.repositories.mapping_rules import MappingRuleRepository
from app.repositories.merchants import MerchantRepository
from app.repositories.shared_state import LlmTxtCacheRepository
from app.services.llm_txt_renderer import render_llm_txt
from app.services.shopify_client import (
    COLLECTIONS_QUERY,
    PRODUCTS_QUERY,
    SHOP_QUERY,
    ShopifyAuthError,
    ShopifyError,
)

router = APIRouter(prefix="/agent-gateway", tags=["agent-gateway"])
log = structlog.get_logger(__name__)

SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")
MEDIA_TYPE = "text/plain; charset=utf-8"


@router.get(
    "/llm.txt",
    response_class=PlainTextResponse,
    response_model=None,
    responses={200: {"content": {"text/plain": {}}}},
    summary="Store catalog as llm.txt (streamed Markdown for LLM ingestion)",
)
async def llm_txt(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    http: HttpClientDep,
    tokens: TokenProviderDep,
    shop: str = Query(..., description="The store's myshopify.com domain"),
) -> StreamingResponse | PlainTextResponse:
    shop = shop.strip().lower()
    if not SHOP_RE.fullmatch(shop):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "shop must be a myshopify.com domain")
    merchant = await MerchantRepository(session).get_active_by_shop(shop)
    if merchant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "store not found")

    headers = {
        "Cache-Control": f"public, max-age={settings.llm_txt_cache_seconds}",
        "X-Robots-Tag": "noindex",
    }
    # Shared by all replicas, so an FAQ edit invalidates every copy.
    cached = await LlmTxtCacheRepository(session, settings.llm_txt_cache_seconds).get(shop)
    if cached is not None:
        return PlainTextResponse(cached, media_type=MEDIA_TYPE, headers=headers)

    client = shopify_client_for(merchant, http, tokens, settings)
    rules = await MappingRuleRepository(session).domain_rules(merchant.id)
    faqs = list(merchant.settings.get("faqs", []))

    # Fetch the shop header before streaming so auth failures become a clean HTTP error.
    try:
        shop_data = (await client.query(SHOP_QUERY))["shop"]
    except ShopifyAuthError as exc:
        log.warning("llm_txt.auth_failed", shop=shop, error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "store temporarily unavailable"
        ) from exc
    except ShopifyError as exc:
        log.warning("llm_txt.shopify_error", shop=shop, error=str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream error") from exc

    async def body() -> AsyncIterator[str]:
        parts: list[str] = []
        try:
            async for chunk in render_llm_txt(
                shop_data,
                client.paginate(COLLECTIONS_QUERY, ["collections"]),
                client.paginate(
                    PRODUCTS_QUERY,
                    ["products"],
                    {"query": "status:active published_status:published"},
                ),
                faqs=faqs,
                rules=rules,
                max_products=settings.llm_txt_max_products,
            ):
                parts.append(chunk)
                yield chunk
        except ShopifyError as exc:
            # Headers are already sent; end the document with a visible marker instead.
            log.warning("llm_txt.stream_aborted", shop=shop, error=str(exc))
            yield "\n_Catalog listing interrupted. Retry shortly._\n"
            return
        # The request's session may already be closed while the body streams.
        async with request.app.state.db.session() as stream_session:
            await LlmTxtCacheRepository(stream_session, settings.llm_txt_cache_seconds).put(
                shop, "".join(parts)
            )

    return StreamingResponse(body(), media_type=MEDIA_TYPE, headers=headers)
