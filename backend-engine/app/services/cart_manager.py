"""Protocol-neutral cart engine for agent checkouts.

Every protocol adapter (UCP, ACP, MCP) translates its wire format into `CartChanges` and back;
this module owns the rules:

- Inventory: each line is checked against the live variant in Shopify (product active, variant
  sellable, tracked stock with a "deny" policy). Over-requests are clamped to what is in stock with
  a warning; unsellable lines stay in the cart flagged unavailable so the agent can see why.
- Pricing: the cart is priced by Shopify itself through `draftOrderCalculate`, which applies the
  merchant's real shipping profiles, tax settings and markets. Nothing is computed locally except
  the per-line share of tax. No draft order is ever created.
- Checkout: the buyer pays on the merchant's own Shopify checkout, never here. A `ready` cart
  carries `checkout_url`, a Shopify cart permalink with the lines, the buyer's email and shipping
  address prefilled, which the agent hands to the buyer.
- State: `open` until the cart has sellable lines (and, when an address is given, Shopify ships
  there); then `ready`. Email and address are optional: they prefill checkout and make the quoted
  shipping and tax exact. `canceled` by the agent, `expired` after `CART_TTL` without changes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from app.models import AgentCart, AgentProtocol, CartStatus
from app.models.base import utcnow
from app.repositories.carts import CartRepository

CART_TTL = timedelta(hours=6)
MAX_LINES = 100
MAX_QUANTITY = 999
CENT = Decimal("0.01")
VARIANT_GID = "gid://shopify/ProductVariant/"


class ShopifyQuery(Protocol):
    """The Shopify Admin GraphQL access the cart engine needs."""

    async def query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL query and return its `data`."""


class CartError(Exception):
    """A request the cart cannot accept. `code` is one of not_found, not_modifiable, invalid."""

    def __init__(self, code: str, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class LineInput:
    """A requested cart line: variant and quantity."""

    variant_id: str
    quantity: int
    # The id the agent used (product feed id, numeric id or GID), echoed back in responses.
    ref: str | None = None


@dataclass(frozen=True)
class BuyerInput:
    """Buyer details used to prefill checkout."""

    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class AddressInput:
    """A shipping address used to quote shipping and tax and to prefill checkout."""

    address1: str
    city: str
    country_code: str
    zip: str | None = None
    province_code: str | None = None
    address2: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class CartChanges:
    """`None` means "leave as is"."""

    lines: list[LineInput] | None = None
    buyer: BuyerInput | None = None
    shipping_address: AddressInput | None = None
    shipping_rate_handle: str | None = None


def variant_gid(ref: str | int) -> str:
    """Accept `123`, `"123"` or a full GID and return the ProductVariant GID."""
    value = str(ref).strip()
    if value.startswith(VARIANT_GID):
        value = value.removeprefix(VARIANT_GID)
    if not value.isdigit():
        raise CartError("invalid", f"unknown item id {ref!r}")
    return VARIANT_GID + value


def line_id(variant: str) -> str:
    """Derive a stable cart line id from a variant id."""
    return "li_" + variant.rsplit("/", 1)[-1]


def money(value: Any) -> Decimal:
    """Round an amount to cents."""
    return Decimal(str(value or "0")).quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- Shopify queries

VARIANTS_QUERY = """
query CartVariants($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      title
      sku
      price
      availableForSale
      inventoryQuantity
      inventoryPolicy
      inventoryItem { tracked requiresShipping }
      image { url }
      product { id title status onlineStoreUrl }
    }
  }
}
"""

CALCULATE_MUTATION = """
mutation CartCalculate($input: DraftOrderInput!) {
  draftOrderCalculate(input: $input) {
    calculatedDraftOrder {
      currencyCode
      taxesIncluded
      subtotalPriceSet { shopMoney { amount currencyCode } }
      totalShippingPriceSet { shopMoney { amount } }
      totalTaxSet { shopMoney { amount } }
      totalPriceSet { shopMoney { amount } }
      availableShippingRates { handle title price { amount currencyCode } }
      shippingLine { title }
      lineItems {
        variant { id }
        quantity
        originalUnitPriceSet { shopMoney { amount } }
        originalTotalSet { shopMoney { amount } }
        discountedTotalSet { shopMoney { amount } }
      }
    }
    userErrors { field message }
  }
}
"""


def _amount(money_set: dict[str, Any] | None) -> Decimal:
    return money(((money_set or {}).get("shopMoney") or {}).get("amount"))


def _message(kind: str, code: str, content: str, path: str | None = None) -> dict[str, Any]:
    return {"type": kind, "code": code, "content": content, "path": path}


# --------------------------------------------------------------------------- manager


class CartManager:
    """Creates, updates and cancels agent carts for one shop, checking stock and pricing with
    Shopify."""

    def __init__(self, carts: CartRepository, shopify: ShopifyQuery, shop_domain: str) -> None:
        self.carts = carts
        self.shopify = shopify
        self.shop_domain = shop_domain

    async def get(self, merchant_id: uuid.UUID, cart_id: uuid.UUID) -> AgentCart:
        """Return a cart, expiring it first if its time-to-live has passed."""
        cart = await self.carts.get(merchant_id, cart_id)
        if cart is None:
            raise CartError("not_found", "checkout session not found")
        if (
            cart.status in (CartStatus.OPEN, CartStatus.READY)
            and cart.expires_at
            and cart.expires_at <= utcnow()
        ):
            cart.status = CartStatus.EXPIRED
            cart.checkout_url = None
            cart = await self.carts.save(cart)
        return cart

    async def create(
        self,
        merchant_id: uuid.UUID,
        protocol: AgentProtocol,
        changes: CartChanges,
        *,
        agent_client_id: uuid.UUID | None = None,
        external_session_id: str | None = None,
    ) -> AgentCart:
        """Create a cart from the requested lines, then check stock and price it."""
        if not changes.lines:
            raise CartError("invalid", "at least one line item is required", "lines")
        cart = AgentCart(
            merchant_id=merchant_id,
            protocol=protocol,
            agent_client_id=agent_client_id,
            external_session_id=external_session_id,
        )
        return await self._apply(cart, changes)

    async def update(self, cart: AgentCart, changes: CartChanges) -> AgentCart:
        """Apply changes to an open or ready cart and re-price it."""
        if cart.status not in (CartStatus.OPEN, CartStatus.READY):
            raise CartError("not_modifiable", f"checkout session is {cart.status.value}")
        return await self._apply(cart, changes)

    async def cancel(self, cart: AgentCart) -> AgentCart:
        """Cancel an open or ready cart."""
        if cart.status not in (CartStatus.OPEN, CartStatus.READY):
            raise CartError("not_modifiable", f"checkout session is {cart.status.value}")
        cart.status = CartStatus.CANCELED
        cart.checkout_url = None
        cart.updated_at = utcnow()
        return await self.carts.save(cart)

    # ------------------------------------------------------------------ internals

    async def _apply(self, cart: AgentCart, changes: CartChanges) -> AgentCart:
        if changes.lines is not None:
            cart.lines = [
                {"variant_id": li.variant_id, "ref": li.ref, "requested_quantity": li.quantity}
                for li in _merge_lines(changes.lines)
            ]
        if changes.buyer is not None:
            cart.buyer = _compact(changes.buyer.__dict__)
        if changes.shipping_address is not None:
            new_address = _compact(changes.shipping_address.__dict__)
            if new_address != cart.shipping_address:
                cart.shipping_rate_handle = None  # rates differ per destination
            cart.shipping_address = new_address
        if changes.shipping_rate_handle is not None:
            cart.shipping_rate_handle = changes.shipping_rate_handle

        await self._reprice(cart, explicit_rate=changes.shipping_rate_handle)
        cart.checkout_url = (
            shopify_checkout_url(self.shop_domain, cart)
            if cart.status is CartStatus.READY
            else None
        )
        now = utcnow()
        cart.updated_at = now
        cart.expires_at = now + CART_TTL
        return await self.carts.save(cart)

    async def _reprice(self, cart: AgentCart, explicit_rate: str | None) -> None:
        messages: list[dict[str, Any]] = []
        lines = await self._check_inventory(cart.lines, messages)
        sellable = [li for li in lines if li["available"] and li["quantity"] > 0]
        ships = any(li["requires_shipping"] for li in sellable)

        options: list[dict[str, Any]] = []
        calc: dict[str, Any] | None = None
        if sellable:
            calc = await self._calculate(
                cart, sellable, cart.shipping_rate_handle if ships else None
            )
            options = [
                {
                    "id": r["handle"],
                    "title": r["title"],
                    "amount": str(money(r["price"]["amount"])),
                }
                for r in calc.get("availableShippingRates") or []
            ]
            handles = {o["id"] for o in options}
            if ships and cart.shipping_address:
                if cart.shipping_rate_handle not in handles:
                    if explicit_rate is not None:
                        messages.append(
                            _message(
                                "error",
                                "invalid",
                                "That shipping option is not available for this address.",
                                "shipping_option",
                            )
                        )
                    cart.shipping_rate_handle = None
                    if options:
                        # Default to the cheapest rate so the agent sees a complete total.
                        cheapest = min(options, key=lambda o: Decimal(o["amount"]))
                        cart.shipping_rate_handle = cheapest["id"]
                        calc = await self._calculate(cart, sellable, cheapest["id"])
            if not ships:
                cart.shipping_rate_handle = None

        self._apply_totals(cart, lines, calc)
        cart.lines = lines
        cart.shipping_options = options

        if not sellable:
            messages.append(
                _message("error", "missing", "Add at least one available item.", "lines")
            )
        if sellable and not (cart.buyer or {}).get("email"):
            messages.append(
                _message(
                    "info", "missing", "Add the buyer's email to prefill checkout.", "buyer.email"
                )
            )
        if ships and not cart.shipping_address:
            messages.append(
                _message(
                    "info",
                    "missing",
                    "Shipping and taxes are finalized at checkout. Add a shipping address "
                    "for an exact quote.",
                    "shipping_address",
                )
            )
        elif ships and not options:
            messages.append(
                _message(
                    "error",
                    "invalid",
                    "The store does not ship to this address.",
                    "shipping_address",
                )
            )

        cart.messages = messages
        blocking = any(m["type"] == "error" for m in messages)
        cart.status = CartStatus.OPEN if blocking else CartStatus.READY

    async def _check_inventory(
        self, lines: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not lines:
            return []
        ids = [li["variant_id"] for li in lines]
        nodes = (await self.shopify.query(VARIANTS_QUERY, {"ids": ids}))["nodes"]
        by_id = {n["id"]: n for n in nodes if n and n.get("id")}

        checked: list[dict[str, Any]] = []
        for li in lines:
            lid = line_id(li["variant_id"])
            requested = int(li["requested_quantity"])
            path = f"line:{lid}"
            variant = by_id.get(li["variant_id"])
            line: dict[str, Any] = {
                "id": lid,
                "variant_id": li["variant_id"],
                "ref": li.get("ref"),
                "requested_quantity": requested,
                "quantity": requested,
                "available": True,
                "requires_shipping": True,
                "unit_price": "0.00",
            }
            product = (variant or {}).get("product") or {}
            if variant is None or product.get("status") != "ACTIVE":
                line.update(available=False, quantity=0, title="Unavailable item")
                messages.append(
                    _message("error", "item_unavailable", "This item is no longer sold.", path)
                )
                checked.append(line)
                continue

            title = product.get("title") or ""
            variant_title = variant.get("title") or ""
            line.update(
                product_id=product.get("id"),
                title=title,
                variant_title=None if variant_title == "Default Title" else variant_title,
                sku=variant.get("sku"),
                unit_price=str(money(variant.get("price"))),
                image_url=(variant.get("image") or {}).get("url"),
                url=product.get("onlineStoreUrl"),
                requires_shipping=bool(
                    (variant.get("inventoryItem") or {}).get("requiresShipping", True)
                ),
            )
            tracked = bool((variant.get("inventoryItem") or {}).get("tracked"))
            stock = variant.get("inventoryQuantity")
            limited = tracked and variant.get("inventoryPolicy") == "DENY" and stock is not None
            if not variant.get("availableForSale") or (limited and stock <= 0):
                line.update(available=False, quantity=0)
                messages.append(
                    _message("error", "out_of_stock", f"{title} is out of stock.", path)
                )
            elif limited and requested > stock:
                line["quantity"] = stock
                messages.append(
                    _message(
                        "warning",
                        "quantity_adjusted",
                        f"Only {stock} of {title} in stock; quantity reduced from {requested}.",
                        path,
                    )
                )
            checked.append(line)
        return checked

    async def _calculate(
        self, cart: AgentCart, lines: list[dict[str, Any]], rate_handle: str | None
    ) -> dict[str, Any]:
        draft = draft_order_input(cart, lines, rate_handle)
        result = (await self.shopify.query(CALCULATE_MUTATION, {"input": draft}))[
            "draftOrderCalculate"
        ]
        errors = result.get("userErrors") or []
        if errors and rate_handle:
            # A stale rate handle is the usual cause; price without it and let the caller pick.
            return await self._calculate(cart, lines, None)
        if errors or not result.get("calculatedDraftOrder"):
            raise CartError("invalid", "; ".join(e["message"] for e in errors) or "cannot price")
        return result["calculatedDraftOrder"]  # type: ignore[no-any-return]

    @staticmethod
    def _apply_totals(
        cart: AgentCart, lines: list[dict[str, Any]], calc: dict[str, Any] | None
    ) -> None:
        if calc is None:
            for li in lines:
                li.update(subtotal="0.00", discount="0.00", tax="0.00", total="0.00")
            cart.subtotal = cart.shipping = cart.tax = cart.total = Decimal("0")
            return

        cart.currency = calc.get("currencyCode") or cart.currency
        cart.subtotal = _amount(calc.get("subtotalPriceSet"))
        cart.shipping = _amount(calc.get("totalShippingPriceSet"))
        cart.tax = _amount(calc.get("totalTaxSet"))
        cart.total = _amount(calc.get("totalPriceSet"))

        priced = {
            li["variant"]["id"]: li for li in calc.get("lineItems") or [] if li.get("variant")
        }
        for li in lines:
            p = priced.get(li["variant_id"])
            if p is None:
                li.update(subtotal="0.00", discount="0.00")
                continue
            original = _amount(p.get("originalTotalSet"))
            discounted = _amount(p.get("discountedTotalSet"))
            li.update(
                unit_price=str(_amount(p.get("originalUnitPriceSet"))),
                subtotal=str(discounted),
                discount=str(original - discounted),
            )

        # Shopify reports tax per order; spread it over lines by value so line totals add up.
        taxes_included = bool(calc.get("taxesIncluded"))
        base = sum((Decimal(li["subtotal"]) for li in lines), Decimal("0"))
        remaining = cart.tax
        shares = [li for li in lines if Decimal(li["subtotal"]) > 0]
        for i, li in enumerate(shares):
            share = (
                remaining
                if i == len(shares) - 1
                else money(cart.tax * Decimal(li["subtotal"]) / base)
            )
            remaining -= share
            total = Decimal(li["subtotal"]) + (Decimal("0") if taxes_included else share)
            li.update(tax=str(share), total=str(total))
        for li in lines:
            li.setdefault("tax", "0.00")
            li.setdefault("total", li["subtotal"])


# Shopify cart permalink fields for the prefilled shipping address.
PERMALINK_ADDRESS = {
    "first_name": "first_name",
    "last_name": "last_name",
    "address1": "address1",
    "address2": "address2",
    "city": "city",
    "province_code": "province",
    "country_code": "country",
    "zip": "zip",
    "phone": "phone",
}


def shopify_checkout_url(shop_domain: str, cart: AgentCart) -> str:
    """A cart permalink that opens the store's own checkout with this cart's sellable lines.

    `https://<shop>/cart/<variant>:<qty>,...` creates the cart and goes straight to checkout;
    `checkout[...]` prefills the buyer, and the hidden `_agentic_cart_id` attribute lands on the
    order so it can be traced back to this cart.
    """
    items = ",".join(
        f"{li['variant_id'].rsplit('/', 1)[-1]}:{li['quantity']}" for li in sellable_lines(cart)
    )
    params: list[tuple[str, str]] = []
    if email := (cart.buyer or {}).get("email"):
        params.append(("checkout[email]", email))
    for key, field in PERMALINK_ADDRESS.items():
        if value := (cart.shipping_address or {}).get(key):
            params.append((f"checkout[shipping_address][{field}]", str(value)))
    params.append(("attributes[_agentic_cart_id]", str(cart.id)))
    return f"https://{shop_domain}/cart/{quote(items, safe=':,')}?{urlencode(params)}"


def draft_order_input(
    cart: AgentCart, lines: list[dict[str, Any]], rate_handle: str | None
) -> dict[str, Any]:
    """`DraftOrderInput` used only to price a cart with `draftOrderCalculate`."""
    draft: dict[str, Any] = {
        "lineItems": [{"variantId": li["variant_id"], "quantity": li["quantity"]} for li in lines]
    }
    buyer = cart.buyer or {}
    if buyer.get("email"):
        draft["email"] = buyer["email"]
    if cart.shipping_address:
        draft["shippingAddress"] = _mailing_address(cart.shipping_address)
    if rate_handle:
        draft["shippingLine"] = {"shippingRateHandle": rate_handle}
    return draft


def sellable_lines(cart: AgentCart) -> list[dict[str, Any]]:
    """Return the cart lines that are available with a positive quantity."""
    return [li for li in cart.lines if li["available"] and li["quantity"] > 0]


def _merge_lines(lines: list[LineInput]) -> list[LineInput]:
    if len(lines) > MAX_LINES:
        raise CartError("invalid", f"a cart holds at most {MAX_LINES} lines", "lines")
    merged: dict[str, LineInput] = {}
    for li in lines:
        if li.quantity < 1 or li.quantity > MAX_QUANTITY:
            raise CartError("invalid", f"quantity must be between 1 and {MAX_QUANTITY}", "lines")
        prev = merged.get(li.variant_id)
        quantity = li.quantity + (prev.quantity if prev else 0)
        merged[li.variant_id] = LineInput(li.variant_id, min(quantity, MAX_QUANTITY), li.ref)
    return list(merged.values())


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v not in (None, "")}


def _mailing_address(address: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "address1": "address1",
        "address2": "address2",
        "city": "city",
        "province_code": "provinceCode",
        "country_code": "countryCode",
        "zip": "zip",
        "first_name": "firstName",
        "last_name": "lastName",
        "phone": "phone",
    }
    return {gql: address[k] for k, gql in keys.items() if address.get(k)}
