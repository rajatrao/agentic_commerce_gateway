from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.security import TokenCipher
from app.services.shopify_client import (
    ShopifyAdminClient,
    ShopifyAuthError,
    ShopifyError,
    ShopifyTokenProvider,
)
from tests.keys import make_keypair

SHOP = "acme.myshopify.com"
URL = f"https://{SHOP}/admin/api/2026-10/graphql.json"
FRONTEND = "http://frontend.internal:3000"


async def _no_sleep(_s: float) -> None:
    return None


def _client(http: httpx.AsyncClient, **kw: Any) -> ShopifyAdminClient:
    async def token() -> str:
        return "shpat_x"

    return ShopifyAdminClient(http, SHOP, token, "2026-10", sleep=_no_sleep, **kw)


async def test_query_retries_throttle_and_5xx() -> None:
    throttled = {
        "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
        "extensions": {
            "cost": {
                "requestedQueryCost": 300,
                "throttleStatus": {"currentlyAvailable": 10, "restoreRate": 100},
            }
        },
    }
    with respx.mock() as router:
        route = router.post(URL).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=throttled),
                httpx.Response(200, json={"data": {"shop": {"name": "Acme"}}}),
            ]
        )
        async with httpx.AsyncClient() as http:
            data = await _client(http).query("{ shop { name } }")
    assert data == {"shop": {"name": "Acme"}}
    assert route.call_count == 3
    assert route.calls[0].request.headers["X-Shopify-Access-Token"] == "shpat_x"


async def test_query_raises_graphql_errors() -> None:
    with respx.mock() as router:
        router.post(URL).respond(200, json={"errors": [{"message": "Field 'x' doesn't exist"}]})
        async with httpx.AsyncClient() as http:
            with pytest.raises(ShopifyError, match="doesn't exist"):
                await _client(http).query("{ x }")


async def test_unauthorized_invalidates_token_once() -> None:
    invalidated: list[bool] = []
    with respx.mock() as router:
        router.post(URL).mock(
            side_effect=[httpx.Response(401), httpx.Response(200, json={"data": {"ok": True}})]
        )
        async with httpx.AsyncClient() as http:
            client = _client(http, on_unauthorized=lambda: invalidated.append(True))
            assert await client.query("{ ok }") == {"ok": True}
    assert invalidated == [True]


async def test_pagination_follows_cursors() -> None:
    pages = [
        {
            "items": {
                "nodes": [{"id": 1}, {"id": 2}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            }
        },
        {"items": {"nodes": [{"id": 3}], "pageInfo": {"hasNextPage": False, "endCursor": None}}},
    ]
    with respx.mock() as router:
        route = router.post(URL).mock(
            side_effect=[httpx.Response(200, json={"data": p}) for p in pages]
        )
        async with httpx.AsyncClient() as http:
            ids = [n["id"] async for n in _client(http).paginate("q", ["items"], {"query": "x"})]
    assert ids == [1, 2, 3]
    import json

    assert json.loads(route.calls[1].request.content)["variables"] == {"query": "x", "cursor": "c1"}


def _settings(**kw: Any) -> Settings:
    private_pem, _ = make_keypair()
    return Settings(
        frontend_internal_url=FRONTEND,
        backend_jwt_private_key=private_pem,
        token_encryption_key=Fernet.generate_key().decode(),
        **kw,
    )


async def test_token_provider_fetches_from_frontend_and_caches() -> None:
    settings = _settings()
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with respx.mock() as router:
        route = router.post(f"{FRONTEND}/internal/admin-token").respond(
            200, json={"access_token": "shpat_fresh", "expires_at": expires}
        )
        async with httpx.AsyncClient() as http:
            provider = ShopifyTokenProvider(http, settings)
            assert await provider.get(SHOP) == "shpat_fresh"
            assert await provider.get(SHOP) == "shpat_fresh"
    assert route.call_count == 1
    bearer = route.calls[0].request.headers["Authorization"].split()[1]
    claims = jwt.decode(bearer, options={"verify_signature": False})
    assert claims["iss"] == "backend-engine" and claims["aud"] == "frontend-ui"
    assert claims["sub"] == SHOP and claims["scope"] == ["admin-token:read"]


async def test_token_provider_refetches_near_expiry() -> None:
    settings = _settings()
    soon = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    with respx.mock() as router:
        route = router.post(f"{FRONTEND}/internal/admin-token").respond(
            200, json={"access_token": "shpat_short", "expires_at": soon}
        )
        async with httpx.AsyncClient() as http:
            provider = ShopifyTokenProvider(http, settings)
            await provider.get(SHOP)
            await provider.get(SHOP)
    assert route.call_count == 2


async def test_token_provider_falls_back_to_stored_token() -> None:
    settings = _settings()
    stored = TokenCipher(settings.token_encryption_key).encrypt("shpat_stored")
    with respx.mock() as router:
        router.post(f"{FRONTEND}/internal/admin-token").mock(side_effect=httpx.ConnectError("x"))
        async with httpx.AsyncClient() as http:
            provider = ShopifyTokenProvider(http, settings)
            assert await provider.get(SHOP, stored) == "shpat_stored"
            with pytest.raises(ShopifyAuthError):
                await ShopifyTokenProvider(http, settings).get(SHOP, None)


async def test_token_provider_404_means_not_installed() -> None:
    with respx.mock() as router:
        router.post(f"{FRONTEND}/internal/admin-token").respond(404)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ShopifyAuthError, match="no offline session"):
                await ShopifyTokenProvider(http, _settings()).get(SHOP)
