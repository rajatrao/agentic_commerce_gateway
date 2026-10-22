"""Model Context Protocol server: catalog search and cart tools for MCP agents.

Built on the official `mcp` SDK (`MCPServer`), served over streamable HTTP in stateless JSON mode
so any replica can answer any request. Like the other adapters, this module only translates: the
API layer supplies a `ToolScopeFactory` that authenticates the request (agent API key) and hands
each tool call a `ToolScope` with the merchant's Shopify client and `CartManager`. There is no
payment tool: a ready cart's `checkout_url` opens the merchant's own Shopify checkout.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app.models import AgentCart, AgentProtocol
from app.protocols.base import EMAIL_PATTERN
from app.services.cart_manager import (
    AddressInput,
    BuyerInput,
    CartChanges,
    CartError,
    CartManager,
    LineInput,
    ShopifyQuery,
    money,
    variant_gid,
)
from app.services.catalog_auditor import plain_text
from app.services.shopify_client import ShopifyAuthError, ShopifyError

INSTRUCTIONS = """\
Shop this store on the buyer's behalf. Use search_products to find items, then create_cart with
variant ids and quantities. Read `messages` on every cart response; they say exactly what is
missing or out of stock. Prices are decimal strings in the cart's currency and come from the
store's own shipping and tax settings; add the buyer's email and shipping address for an exact
quote and a prefilled checkout. When the cart's status is "ready", give the buyer its
`checkout_url`: they review and pay on the store's own checkout. You never handle payment."""

SEARCH_QUERY = """
query AgentSearch($query: String!, $first: Int!) {
  products(first: $first, query: $query, sortKey: RELEVANCE) {
    nodes {
      id
      title
      handle
      vendor
      productType
      description
      onlineStoreUrl
      priceRangeV2 {
        minVariantPrice { amount currencyCode }
        maxVariantPrice { amount currencyCode }
      }
      featuredMedia { preview { image { url } } }
      variants(first: 25) { nodes { id title sku price availableForSale } }
    }
  }
}
"""

PRODUCT_QUERY = """
query AgentProduct($id: ID!) {
  product(id: $id) {
    id
    title
    handle
    vendor
    productType
    status
    descriptionHtml
    onlineStoreUrl
    options { name values }
    featuredMedia { preview { image { url } } }
    variants(first: 100) {
      nodes {
        id title sku price compareAtPrice availableForSale
        selectedOptions { name value }
      }
    }
  }
}
"""


@dataclass
class ToolScope:
    """What an MCP tool call may act on: the agent's merchant, scopes, Shopify client and cart
    manager."""

    merchant_id: uuid.UUID
    agent_client_id: uuid.UUID
    scopes: frozenset[str]
    shopify: ShopifyQuery
    carts: CartManager

    def require(self, scope: str) -> None:
        """Raise a `ToolError` unless the agent key holds `scope`."""
        if scope not in self.scopes:
            raise ToolError(f"this API key lacks the {scope!r} scope")


# Given the tool call's Context, open a ToolScope for the authenticated agent.
ToolScopeFactory = Callable[[Context], AbstractAsyncContextManager[ToolScope]]


class CartLine(BaseModel):
    """A line an MCP agent adds to a cart."""

    variant_id: str = Field(description="Variant id from search_products (numeric or GID)")
    quantity: int = Field(ge=1, le=999)


class ShippingAddress(BaseModel):
    """A shipping address an MCP agent sends to price shipping and prefill checkout."""

    first_name: str | None = None
    last_name: str | None = None
    address1: str = Field(min_length=1)
    address2: str | None = None
    city: str = Field(min_length=1)
    province_code: str | None = Field(default=None, description="e.g. CA, ON")
    country_code: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2")
    zip: str | None = None
    phone: str | None = None


Email = Annotated[str, Field(pattern=EMAIL_PATTERN, max_length=256)]


def render_cart(cart: AgentCart) -> dict[str, Any]:
    """Render a cart as the MCP tools' JSON result."""
    return {
        "cart_id": str(cart.id),
        "status": cart.status.value,
        "currency": cart.currency,
        "lines": [
            {
                "line_id": li["id"],
                "variant_id": li["variant_id"],
                "title": li.get("title"),
                "variant_title": li.get("variant_title"),
                "quantity": li["quantity"],
                "unit_price": li["unit_price"],
                "total": li.get("total", "0.00"),
                "available": li["available"],
            }
            for li in cart.lines
        ],
        "subtotal": str(money(cart.subtotal)),
        "shipping": str(money(cart.shipping)),
        "tax": str(money(cart.tax)),
        "total": str(money(cart.total)),
        "buyer_email": (cart.buyer or {}).get("email"),
        "shipping_address": cart.shipping_address,
        "shipping_options": cart.shipping_options,
        "selected_shipping_option": cart.shipping_rate_handle,
        "messages": [
            {k: m[k] for k in ("type", "code", "content", "path") if m.get(k)}
            for m in cart.messages
        ],
        "checkout_url": cart.checkout_url,
        "order": (
            {"id": cart.order_gid, "name": cart.order_name, "status_url": cart.order_status_url}
            if cart.order_gid
            else None
        ),
        "expires_at": cart.expires_at.isoformat() if cart.expires_at else None,
    }


def render_product(node: dict[str, Any]) -> dict[str, Any]:
    """Render a Shopify product node as the MCP tools' product result."""
    price = node.get("priceRangeV2") or {}
    low = (price.get("minVariantPrice") or {}).get("amount")
    high = (price.get("maxVariantPrice") or {}).get("amount")
    description = node.get("description") or plain_text(node.get("descriptionHtml") or "")
    return {
        "product_id": node["id"],
        "title": node.get("title"),
        "vendor": node.get("vendor") or None,
        "product_type": node.get("productType") or None,
        "url": node.get("onlineStoreUrl"),
        "image_url": (
            ((node.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}
        ).get("url"),
        "price_range": {
            "min": str(money(low)) if low else None,
            "max": str(money(high)) if high else None,
            "currency": (price.get("minVariantPrice") or {}).get("currencyCode"),
        },
        "options": node.get("options"),
        "description": description[:1000],
        "variants": [
            {
                "variant_id": v["id"],
                "title": v.get("title"),
                "sku": v.get("sku") or None,
                "price": str(money(v.get("price"))),
                "available": bool(v.get("availableForSale")),
                **(
                    {"options": {o["name"]: o["value"] for o in v["selectedOptions"]}}
                    if v.get("selectedOptions")
                    else {}
                ),
            }
            for v in (node.get("variants") or {}).get("nodes", [])
        ],
    }


def _lines(lines: list[CartLine]) -> list[LineInput]:
    return [LineInput(variant_gid(li.variant_id), li.quantity, ref=li.variant_id) for li in lines]


def _address(address: ShippingAddress | None) -> AddressInput | None:
    if address is None:
        return None
    return AddressInput(**{**address.model_dump(), "country_code": address.country_code.upper()})


def _cart_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ToolError("cart not found") from None


class _ToolErrors:
    """Turn domain and upstream failures into MCP tool errors the agent can read."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        if isinstance(exc, CartError):
            raise ToolError(exc.message) from exc
        if isinstance(exc, ShopifyAuthError):
            raise ToolError("the store is temporarily unavailable") from exc
        if isinstance(exc, ShopifyError):
            raise ToolError("the store could not answer; retry shortly") from exc


def build_mcp_server(open_scope: ToolScopeFactory) -> MCPServer:
    """Build the MCP server with its catalog and cart tools, each run inside a `ToolScope`."""
    server = MCPServer(
        name="agentic-commerce-gateway",
        title="Store checkout",
        instructions=INSTRUCTIONS,
        version="0.3.0",
    )
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    @server.tool(annotations=read_only)
    async def search_products(
        ctx: Context, query: str = "", limit: Annotated[int, Field(ge=1, le=25)] = 10
    ) -> dict[str, Any]:
        """Search the store's published, active products. Returns variants with ids and prices."""
        async with open_scope(ctx) as scope, _ToolErrors():
            scope.require("catalog:read")
            search = " ".join(
                p for p in ("status:active published_status:published", query.strip()[:200]) if p
            )
            data = await scope.shopify.query(SEARCH_QUERY, {"query": search, "first": limit})
            return {"products": [render_product(n) for n in data["products"]["nodes"]]}

    @server.tool(annotations=read_only)
    async def get_product(ctx: Context, product_id: str) -> dict[str, Any]:
        """Full details for one product, including every variant and its options."""
        async with open_scope(ctx) as scope, _ToolErrors():
            scope.require("catalog:read")
            gid = (
                product_id
                if product_id.startswith("gid://")
                else (f"gid://shopify/Product/{product_id}")
            )
            node = (await scope.shopify.query(PRODUCT_QUERY, {"id": gid}))["product"]
            if not node or node.get("status") != "ACTIVE":
                raise ToolError("product not found")
            return render_product(node)

    @server.tool()
    async def create_cart(
        ctx: Context,
        lines: Annotated[list[CartLine], Field(min_length=1, max_length=100)],
        email: Email | None = None,
        shipping_address: ShippingAddress | None = None,
    ) -> dict[str, Any]:
        """Create a cart. Stock is checked and the cart is priced with the store's real
        shipping rates and taxes; the cheapest shipping option is preselected."""
        async with open_scope(ctx) as scope, _ToolErrors():
            scope.require("checkout")
            cart = await scope.carts.create(
                scope.merchant_id,
                AgentProtocol.MCP,
                CartChanges(
                    lines=_lines(lines),
                    buyer=BuyerInput(email=email) if email else None,
                    shipping_address=_address(shipping_address),
                ),
                agent_client_id=scope.agent_client_id,
            )
            return render_cart(cart)

    @server.tool()
    async def update_cart(
        ctx: Context,
        cart_id: str,
        lines: Annotated[list[CartLine] | None, Field(max_length=100)] = None,
        email: Email | None = None,
        shipping_address: ShippingAddress | None = None,
        shipping_option_id: str | None = None,
    ) -> dict[str, Any]:
        """Change a cart. `lines` replaces all lines; omitted fields are kept."""
        async with open_scope(ctx) as scope, _ToolErrors():
            scope.require("checkout")
            cart = await scope.carts.get(scope.merchant_id, _cart_id(cart_id))
            cart = await scope.carts.update(
                cart,
                CartChanges(
                    lines=_lines(lines) if lines else None,
                    buyer=BuyerInput(email=email) if email else None,
                    shipping_address=_address(shipping_address),
                    shipping_rate_handle=shipping_option_id,
                ),
            )
            return render_cart(cart)

    @server.tool(annotations=read_only)
    async def get_cart(ctx: Context, cart_id: str) -> dict[str, Any]:
        """Current state of a cart."""
        async with open_scope(ctx) as scope, _ToolErrors():
            scope.require("checkout")
            return render_cart(await scope.carts.get(scope.merchant_id, _cart_id(cart_id)))

    @server.tool()
    async def cancel_cart(ctx: Context, cart_id: str) -> dict[str, Any]:
        """Abandon a cart."""
        async with open_scope(ctx) as scope, _ToolErrors():
            scope.require("checkout")
            cart = await scope.carts.get(scope.merchant_id, _cart_id(cart_id))
            return render_cart(await scope.carts.cancel(cart))

    return server
