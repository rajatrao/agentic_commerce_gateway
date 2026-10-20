"""Shopify Admin GraphQL client and offline-token provider.

Token ownership: frontend-ui (Shopify's app library + Prisma) owns the merchant's offline session,
including refreshing expiring offline tokens. backend-engine never refreshes tokens itself (that
would rotate the refresh token underneath frontend-ui). Instead `ShopifyTokenProvider` asks
frontend-ui for a currently valid token and caches it until shortly before expiry. The encrypted
token synced at install time is only a fallback for non-expiring tokens.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from app.core.config import Settings
from app.core.security import AuthError, TokenCipher, mint_backend_service_jwt

log = structlog.get_logger(__name__)

TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
TOKEN_DEFAULT_TTL = timedelta(minutes=10)


class ShopifyError(Exception):
    """Shopify returned an error we cannot recover from."""


class ShopifyAuthError(ShopifyError):
    """No valid Admin API token is available for this shop."""


@dataclass
class _CachedToken:
    value: str
    refresh_after: datetime


class ShopifyTokenProvider:
    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings
        self._cache: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def invalidate(self, shop: str) -> None:
        self._cache.pop(shop, None)

    async def get(self, shop: str, stored_token_enc: bytes | None = None) -> str:
        cached = self._cache.get(shop)
        now = datetime.now(UTC)
        if cached and cached.refresh_after > now:
            return cached.value

        lock = self._locks.setdefault(shop, asyncio.Lock())
        async with lock:
            cached = self._cache.get(shop)
            if cached and cached.refresh_after > datetime.now(UTC):
                return cached.value
            token = await self._fetch_from_frontend(shop)
            if token is None:
                token = self._from_stored(stored_token_enc)
            self._cache[shop] = token
            return token.value

    async def _fetch_from_frontend(self, shop: str) -> _CachedToken | None:
        base = self._settings.frontend_internal_url.rstrip("/")
        if not base or not self._settings.backend_jwt_private_key:
            return None
        bearer = mint_backend_service_jwt(shop, ["admin-token:read"], self._settings)
        try:
            resp = await self._http.post(
                f"{base}/internal/admin-token",
                json={"shop": shop},
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            log.warning("shopify.token.frontend_unreachable", shop=shop, error=str(exc))
            return None
        if resp.status_code == 404:
            raise ShopifyAuthError(f"{shop} has no offline session (app not installed?)")
        if resp.status_code != 200:
            log.warning("shopify.token.frontend_error", shop=shop, status=resp.status_code)
            return None
        data = resp.json()
        expires_raw = data.get("expires_at")
        refresh_after = datetime.now(UTC) + TOKEN_DEFAULT_TTL
        if expires_raw:
            expires = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
            refresh_after = min(refresh_after, expires - TOKEN_REFRESH_MARGIN)
        return _CachedToken(value=data["access_token"], refresh_after=refresh_after)

    def _from_stored(self, stored_token_enc: bytes | None) -> _CachedToken:
        if not stored_token_enc:
            raise ShopifyAuthError("no Admin API token available")
        try:
            value = TokenCipher(self._settings.token_encryption_key).decrypt(stored_token_enc)
        except (AuthError, ValueError) as exc:
            raise ShopifyAuthError(str(exc)) from exc
        # Short TTL: the stored copy may be an expiring token that is already stale.
        return _CachedToken(value=value, refresh_after=datetime.now(UTC) + timedelta(minutes=1))


TokenGetter = Callable[[], Awaitable[str]]


class ShopifyAdminClient:
    """Minimal GraphQL Admin API client with throttle-aware retries and cursor pagination."""

    MAX_ATTEMPTS = 5

    def __init__(
        self,
        http: httpx.AsyncClient,
        shop: str,
        token_getter: TokenGetter,
        api_version: str,
        *,
        on_unauthorized: Callable[[], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._shop = shop
        self._token_getter = token_getter
        self._url = f"https://{shop}/admin/api/{api_version}/graphql.json"
        self._on_unauthorized = on_unauthorized
        self._sleep = sleep

    async def query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error = "unknown"
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            token = await self._token_getter()
            try:
                resp = await self._http.post(
                    self._url,
                    json={"query": query, "variables": variables or {}},
                    headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
                    timeout=30.0,
                )
            except httpx.TransportError as exc:
                last_error = str(exc)
                await self._sleep(min(2**attempt, 20))
                continue

            if resp.status_code == 401 and attempt == 1 and self._on_unauthorized:
                self._on_unauthorized()  # token was stale: drop cache and retry once
                continue
            if resp.status_code in (401, 403):
                raise ShopifyAuthError(f"Shopify rejected the token ({resp.status_code})")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                retry_after = float(resp.headers.get("Retry-After", 2**attempt))
                await self._sleep(min(retry_after, 20))
                continue
            if resp.status_code != 200:
                raise ShopifyError(f"Shopify GraphQL HTTP {resp.status_code}: {resp.text[:300]}")

            body = resp.json()
            errors = body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                last_error = "THROTTLED"
                await self._sleep(self._throttle_wait(body))
                continue
            if errors:
                raise ShopifyError(f"Shopify GraphQL errors: {errors}")
            return body["data"]  # type: ignore[no-any-return]
        raise ShopifyError(f"Shopify GraphQL failed after retries: {last_error}")

    @staticmethod
    def _throttle_wait(body: dict[str, Any]) -> float:
        cost = (body.get("extensions") or {}).get("cost") or {}
        status = cost.get("throttleStatus") or {}
        requested = cost.get("requestedQueryCost") or 100
        available = status.get("currentlyAvailable") or 0
        restore = status.get("restoreRate") or 50
        return float(max(1.0, min((requested - available) / restore, 20.0)))

    async def paginate(
        self, query: str, connection_path: list[str], variables: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every node of a connection (`nodes` + `pageInfo { hasNextPage endCursor }`)."""
        cursor: str | None = None
        while True:
            data = await self.query(query, {**(variables or {}), "cursor": cursor})
            conn: Any = data
            for key in connection_path:
                conn = conn[key]
            for node in conn["nodes"]:
                yield node
            page = conn["pageInfo"]
            if not page["hasNextPage"]:
                return
            cursor = page["endCursor"]


# --------------------------------------------------------------------------- queries

PRODUCT_FIELDS = """
  id
  title
  handle
  status
  vendor
  productType
  tags
  description
  descriptionHtml
  onlineStoreUrl
  updatedAt
  category { id fullName }
  seo { title description }
  options { name values }
  priceRangeV2 {
    minVariantPrice { amount currencyCode }
    maxVariantPrice { amount currencyCode }
  }
  totalInventory
  media(first: 10) {
    nodes {
      mediaContentType
      alt
      ... on MediaImage { image { url altText } }
    }
  }
  variants(first: 50) {
    nodes {
      id
      title
      sku
      barcode
      price
      compareAtPrice
      availableForSale
      inventoryQuantity
      inventoryItem { tracked }
    }
  }
  metafields(first: 30) {
    nodes { namespace key type value }
  }
"""

# Page size is bounded by Shopify's 1,000-point single-query cost limit: each product costs
# roughly 100 points with its variants, media and metafields connections.
PRODUCTS_QUERY = f"""
query AuditProducts($cursor: String, $query: String) {{
  products(first: 8, after: $cursor, query: $query, sortKey: ID) {{
    nodes {{ {PRODUCT_FIELDS} }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""

PRODUCT_QUERY = f"""
query EnrichProduct($id: ID!) {{
  product(id: $id) {{ {PRODUCT_FIELDS} }}
}}
"""

SHOP_QUERY = """
query ShopForLlmTxt {
  shop {
    name
    description
    url
    primaryDomain { url host }
    currencyCode
    contactEmail
    shopPolicies { type title url body }
  }
}
"""

COLLECTIONS_QUERY = """
query CollectionsForLlmTxt($cursor: String) {
  collections(first: 100, after: $cursor, sortKey: TITLE) {
    nodes { id title handle description productsCount { count } }
    pageInfo { hasNextPage endCursor }
  }
}
"""
