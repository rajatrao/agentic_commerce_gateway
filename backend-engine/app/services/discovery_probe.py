"""Run each schema's shopper questions against the store's real search and measure retrieval.

The readiness audit only checks that products carry the attributes a question needs. This
asks the store the question the way an AI shopping agent does: through the Storefront MCP
server every Shopify store exposes at `/api/mcp` (tool `search_shop_catalog`). When that is not
reachable (for example a password-protected development store), it falls back to the Admin
API product search, which is keyword-based, and says so in the result.

For each question it reports what came back and how well it matches the products that should
answer it: those in the question's schema that carry every attribute the question needs
("answerable"). Recall is the share of answerable products found within the top results.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.services.shopify_client import ShopifyAdminClient, ShopifyError

log = structlog.get_logger(__name__)

GID_RE = re.compile(r"gid://shopify/Product/\d+")

ADMIN_SEARCH_QUERY = """
query DiscoverySearch($query: String!, $first: Int!) {
  products(first: $first, query: $query, sortKey: RELEVANCE) {
    nodes { id title }
  }
}
"""


class ProbeUnavailable(Exception):
    """The search channel cannot be used for this store (fall back or give up)."""


@dataclass
class SearchHit:
    """A product a store search returned."""

    product_gid: str
    title: str


@dataclass
class QueryResult:
    """The products one shopper question returned, and how they were searched."""

    schema_key: str
    query: str
    attributes: list[str]
    method: str
    hits: list[SearchHit] = field(default_factory=list)
    error: str | None = None

    def measure(
        self, schema_gids: set[str], answerable: dict[str, str], limit: int
    ) -> dict[str, Any]:
        """Compute recall and precision of the top results against the products that can answer the
        question."""
        top = self.hits[:limit]
        found = [h for h in top if h.product_gid in answerable]
        missed = [
            title for gid, title in answerable.items() if gid not in {h.product_gid for h in top}
        ]
        expected = min(len(answerable), limit)
        return {
            "schema_key": self.schema_key,
            "query": self.query,
            "attributes": self.attributes,
            "method": self.method,
            "error": self.error,
            "results": [{"product_gid": h.product_gid, "title": h.title} for h in top],
            "returned": len(top),
            "in_schema": sum(1 for h in top if h.product_gid in schema_gids),
            "answerable_products": len(answerable),
            "answerable_found": len(found),
            "recall": round(len(found) / expected, 2) if expected else None,
            "precision": round(len(found) / len(top), 2) if top else None,
            "missed_examples": missed[:5],
        }


def _mcp_products(body: Any) -> list[SearchHit]:
    """Products from a `search_shop_catalog` result. The tool returns its data as JSON text
    content; anything else is read for product ids without trusting its structure."""
    result = (body or {}).get("result") or {}
    if (body or {}).get("error") or result.get("isError"):
        raise ProbeUnavailable(str((body or {}).get("error") or "search tool returned an error"))
    hits: dict[str, SearchHit] = {}
    for content in result.get("content") or []:
        text = content.get("text") if isinstance(content, dict) else None
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        products = data.get("products") if isinstance(data, dict) else None
        if isinstance(products, list):
            for p in products:
                if not isinstance(p, dict):
                    continue
                gid = str(p.get("product_id") or p.get("id") or "")
                if GID_RE.fullmatch(gid) and gid not in hits:
                    hits[gid] = SearchHit(gid, str(p.get("title") or ""))
        else:
            for gid in GID_RE.findall(text):
                hits.setdefault(gid, SearchHit(gid, ""))
    return list(hits.values())


def _parse_body(response: httpx.Response) -> Any:
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ProbeUnavailable("empty event stream")
    return response.json()


class DiscoveryProbe:
    """Asks the store's search a shopper question, through Storefront MCP with Admin search as the
    fallback."""

    def __init__(
        self, http: httpx.AsyncClient, shop_domain: str, admin: ShopifyAdminClient, limit: int
    ) -> None:
        self.http = http
        self.url = f"https://{shop_domain}/api/mcp"
        self.admin = admin
        self.limit = limit
        self.method = "storefront_mcp"
        self.fallback_reason: str | None = None
        self._calls = 0

    async def _storefront(self, query: str) -> list[SearchHit]:
        self._calls += 1
        try:
            response = await self.http.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": self._calls,
                    "method": "tools/call",
                    "params": {
                        "name": "search_shop_catalog",
                        "arguments": {
                            "query": query,
                            "context": "Catalog discoverability test run by the merchant's app.",
                        },
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
                timeout=20.0,
            )
        except httpx.HTTPError as exc:
            raise ProbeUnavailable(f"storefront search unreachable: {exc}") from exc
        if response.status_code != 200:
            raise ProbeUnavailable(f"storefront search returned HTTP {response.status_code}")
        try:
            return _mcp_products(_parse_body(response))
        except (ValueError, AttributeError) as exc:
            raise ProbeUnavailable("storefront search returned an unreadable response") from exc

    async def _admin(self, query: str) -> list[SearchHit]:
        data = await self.admin.query(ADMIN_SEARCH_QUERY, {"query": query, "first": self.limit})
        return [
            SearchHit(n["id"], n.get("title") or "")
            for n in (data.get("products") or {}).get("nodes") or []
        ]

    async def search(self, schema_key: str, query: str, attributes: list[str]) -> QueryResult:
        """Run one shopper question and return the products the store's search found."""
        result = QueryResult(schema_key, query, attributes, self.method)
        if self.method == "storefront_mcp":
            try:
                result.hits = await self._storefront(query)
                return result
            except ProbeUnavailable as exc:
                # After a failure the rest of the run uses the fallback; each result records
                # which search produced it.
                log.info("discovery.storefront_unavailable", url=self.url, error=str(exc))
                self.method = result.method = "admin_search"
                self.fallback_reason = str(exc)
        try:
            result.hits = await self._admin(query)
        except ShopifyError as exc:
            result.error = f"search failed: {exc}"
        return result
