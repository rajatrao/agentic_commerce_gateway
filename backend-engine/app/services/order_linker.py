"""Links Shopify orders back to the agent carts they came from.

A cart's checkout link carries the hidden `_agentic_cart_id` cart attribute, which Shopify copies
onto the order as a note attribute. The orders/create webhook reads it and marks the cart
`completed` with the order's id, name and status page. Order payloads hold protected customer
data, so only `order_summary` (no names, emails or addresses) is ever stored.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlmodel.ext.asyncio.session import AsyncSession

from app.repositories.carts import CartRepository
from app.repositories.merchants import MerchantRepository

CART_ATTRIBUTE = "_agentic_cart_id"
ORDER_TOPICS = {"orders/create"}

log = structlog.get_logger(__name__)


def agentic_cart_id(order: dict[str, Any]) -> uuid.UUID | None:
    """Return the agent cart id from an order's `_agentic_cart_id` note attribute, if present."""
    for attr in order.get("note_attributes") or []:
        if isinstance(attr, dict) and attr.get("name") == CART_ATTRIBUTE:
            try:
                return uuid.UUID(str(attr.get("value")))
            except ValueError:
                return None
    return None


def order_gid(order: dict[str, Any]) -> str | None:
    """Return the order's Admin GraphQL id."""
    if gid := order.get("admin_graphql_api_id"):
        return str(gid)
    return f"gid://shopify/Order/{order['id']}" if order.get("id") else None


def order_summary(order: dict[str, Any]) -> dict[str, Any]:
    """What gets stored from an order webhook: identifiers and the cart link, nothing personal."""
    cart_id = agentic_cart_id(order)
    return {
        "id": order.get("id"),
        "admin_graphql_api_id": order_gid(order),
        "name": order.get("name"),
        "agentic_cart_id": str(cart_id) if cart_id else None,
    }


async def link_order(session: AsyncSession, shop_domain: str, order: dict[str, Any]) -> bool:
    """Mark the cart behind this order completed. False when the order didn't come from an agent."""
    cart_id = agentic_cart_id(order)
    gid = order_gid(order)
    if not (cart_id and gid):
        return False
    merchant = await MerchantRepository(session).get_by_shop(shop_domain)
    if merchant is None:
        return False
    linked = await CartRepository(session).link_order(
        merchant.id,
        cart_id,
        order_gid=gid,
        order_name=order.get("name"),
        order_status_url=order.get("order_status_url"),
    )
    log.info(
        "order.linked" if linked else "order.not_linked", shop=shop_domain, cart_id=str(cart_id)
    )
    return linked


async def redact_customer(session: AsyncSession, shop_domain: str, body: dict[str, Any]) -> int:
    """customers/redact: clear buyer details on carts for that customer or their orders."""
    merchant = await MerchantRepository(session).get_by_shop(shop_domain)
    if merchant is None:
        return 0
    email = (body.get("customer") or {}).get("email")
    gids = [f"gid://shopify/Order/{oid}" for oid in body.get("orders_to_redact") or []]
    return await CartRepository(session).redact_buyer(merchant.id, email=email, order_gids=gids)
