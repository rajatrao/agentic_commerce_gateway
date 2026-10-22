"""Universal Commerce Protocol checkout adapter (`dev.ucp.shopping.checkout`, REST binding).

Spec: https://ucp.dev/specification/shopping/checkout/rest/. Amounts are integers in minor units.
Updates are full replacements in the spec; fields an agent omits are kept here rather than
cleared, which is the lenient reading. This business declares no payment handlers: a ready
checkout is `requires_escalation` with `continue_url`, the merchant's own Shopify checkout, where
the buyer pays.
"""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.models import AgentCart, AgentProtocol, CartStatus
from app.protocols.base import (
    EMAIL_PATTERN,
    ProtocolAdapter,
    ProtocolError,
    to_minor,
)
from app.services.cart_manager import (
    AddressInput,
    BuyerInput,
    CartChanges,
    CartError,
    LineInput,
    variant_gid,
)

UCP_VERSION = "2026-08-25"
CHECKOUT_CAPABILITY = "dev.ucp.shopping.checkout"
FULFILLMENT_CAPABILITY = "dev.ucp.shopping.fulfillment"
SHIPPING_METHOD_ID = "shipping"
DESTINATION_ID = "dest_1"
GROUP_ID = "group_1"

STATUS = {
    CartStatus.OPEN: "incomplete",
    # The buyer completes on the merchant's Shopify checkout (continue_url), never via the agent.
    CartStatus.READY: "requires_escalation",
    CartStatus.COMPLETED: "completed",
    CartStatus.CANCELED: "canceled",
    CartStatus.EXPIRED: "canceled",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class UcpItemRef(_Model):
    """Reference to a product or variant in a UCP line item."""

    id: str = Field(min_length=1, max_length=255)


class UcpLineItem(_Model):
    """A UCP line item: an item reference and a quantity."""

    id: str | None = None
    item: UcpItemRef
    quantity: int = Field(ge=1, le=999)


class UcpBuyer(_Model):
    """Buyer details a UCP agent may send to prefill checkout."""

    email: str | None = Field(default=None, max_length=256, pattern=EMAIL_PATTERN)
    first_name: str | None = Field(default=None, max_length=256)
    last_name: str | None = Field(default=None, max_length=256)
    phone_number: str | None = Field(default=None, max_length=32)


class UcpDestination(_Model):
    """A UCP shipping destination (postal address)."""

    id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    street_address: str = Field(min_length=1, max_length=255)
    extended_address: str | None = None
    address_locality: str = Field(min_length=1, max_length=255)
    address_region: str | None = None
    postal_code: str | None = None
    address_country: str = Field(min_length=2, max_length=2)
    phone_number: str | None = None


class UcpGroup(_Model):
    """A UCP fulfillment group with the agent's selected shipping option."""

    id: str | None = None
    selected_option_id: str | None = None


class UcpMethod(_Model):
    """A UCP fulfillment method: one shipping destination and its groups."""

    id: str | None = None
    type: str = "shipping"
    destinations: list[UcpDestination] = Field(default_factory=list, max_length=1)
    selected_destination_id: str | None = None
    groups: list[UcpGroup] = Field(default_factory=list)


class UcpFulfillment(_Model):
    """The UCP fulfillment extension of a checkout request."""

    methods: list[UcpMethod] = Field(default_factory=list)


class CreateCheckout(_Model):
    """Body of a UCP create-checkout request."""

    line_items: list[UcpLineItem] = Field(min_length=1, max_length=100)
    buyer: UcpBuyer | None = None
    fulfillment: UcpFulfillment | None = None


class UpdateCheckout(_Model):
    """Body of a UCP update-checkout request. Omitted fields keep their current values."""

    line_items: list[UcpLineItem] | None = Field(default=None, min_length=1, max_length=100)
    buyer: UcpBuyer | None = None
    fulfillment: UcpFulfillment | None = None


def envelope(status: str | None = None) -> dict[str, Any]:
    """Build the `ucp` metadata block (version and capabilities) included in every UCP response."""
    ucp: dict[str, Any] = {
        "version": UCP_VERSION,
        "capabilities": {
            CHECKOUT_CAPABILITY: [{"version": UCP_VERSION}],
            FULFILLMENT_CAPABILITY: [{"version": UCP_VERSION}],
        },
    }
    if status:
        ucp["status"] = status
    return ucp


class UcpAdapter(ProtocolAdapter):
    """Translates UCP checkouts to `CartChanges` and renders carts as UCP checkouts."""

    protocol: ClassVar[AgentProtocol] = AgentProtocol.UCP
    create_model: ClassVar[type[BaseModel]] = CreateCheckout
    update_model: ClassVar[type[BaseModel]] = UpdateCheckout

    def _changes(self, request: BaseModel) -> CartChanges:
        assert isinstance(request, CreateCheckout | UpdateCheckout)
        lines = None
        if request.line_items is not None:
            lines = []
            for i, li in enumerate(request.line_items):
                try:
                    lines.append(LineInput(variant_gid(li.item.id), li.quantity, ref=li.item.id))
                except CartError as exc:
                    raise ProtocolError(
                        400, "invalid", exc.message, f"$.line_items[{i}].item.id"
                    ) from exc
        buyer = None
        if b := request.buyer:
            buyer = BuyerInput(
                email=b.email, first_name=b.first_name, last_name=b.last_name, phone=b.phone_number
            )
        address, rate = None, None
        shipping = next(
            (
                m
                for m in (request.fulfillment.methods if request.fulfillment else [])
                if m.type == "shipping"
            ),
            None,
        )
        if shipping:
            if shipping.destinations:
                d = shipping.destinations[0]
                address = AddressInput(
                    first_name=d.first_name,
                    last_name=d.last_name,
                    address1=d.street_address,
                    address2=d.extended_address,
                    city=d.address_locality,
                    province_code=d.address_region,
                    zip=d.postal_code,
                    country_code=d.address_country.upper(),
                    phone=d.phone_number,
                )
            rate = next(
                (g.selected_option_id for g in shipping.groups if g.selected_option_id), None
            )
        return CartChanges(
            lines=lines, buyer=buyer, shipping_address=address, shipping_rate_handle=rate
        )

    def render(self, cart: AgentCart) -> dict[str, Any]:
        """Render a cart as a UCP checkout, with amounts in minor units."""
        cur = cart.currency
        lines = cart.lines
        line_items = [
            {
                "id": li["id"],
                "item": {
                    k: v
                    for k, v in {
                        "id": li.get("ref") or li["variant_id"].rsplit("/", 1)[-1],
                        "title": _title(li),
                        "price": to_minor(li["unit_price"], cur),
                        "image_url": li.get("image_url"),
                    }.items()
                    if v is not None
                },
                "quantity": li["quantity"],
                "totals": [
                    {"type": "subtotal", "amount": to_minor(li["subtotal"], cur)},
                    {"type": "tax", "amount": to_minor(li["tax"], cur)},
                    {"type": "total", "amount": to_minor(li["total"], cur)},
                ],
            }
            for li in lines
        ]
        discount = sum(to_minor(li.get("discount", "0"), cur) for li in lines)
        totals = [{"type": "subtotal", "amount": to_minor(cart.subtotal, cur)}]
        if discount:
            totals.append({"type": "discount", "amount": discount})
        totals += [
            {"type": "fulfillment", "amount": to_minor(cart.shipping, cur)},
            {"type": "tax", "amount": to_minor(cart.tax, cur)},
            {"type": "total", "amount": to_minor(cart.total, cur)},
        ]
        body: dict[str, Any] = {
            "ucp": envelope(),
            "id": str(cart.id),
            "status": STATUS[cart.status],
            "currency": cur.upper(),
            "line_items": line_items,
            "totals": totals,
            "messages": [_message(m, lines) for m in cart.messages],
            "links": [],
        }
        if cart.status is CartStatus.READY and cart.checkout_url:
            body["continue_url"] = cart.checkout_url
            body["messages"].append(
                {
                    "type": "error",
                    "code": "requires_buyer_review",
                    "content": "The buyer reviews and pays on the store's own checkout: "
                    "send them to continue_url.",
                    "severity": "requires_buyer_review",
                }
            )
        if cart.order_gid:
            body["order"] = {
                "id": cart.order_gid,
                **({"permalink_url": cart.order_status_url} if cart.order_status_url else {}),
            }
        if cart.expires_at:
            body["expires_at"] = cart.expires_at.isoformat()
        if b := cart.buyer:
            body["buyer"] = {
                k: v
                for k, v in {
                    "email": b.get("email"),
                    "first_name": b.get("first_name"),
                    "last_name": b.get("last_name"),
                    "phone_number": b.get("phone"),
                }.items()
                if v
            }
        shippable = [li["id"] for li in lines if li.get("requires_shipping") and li["available"]]
        if shippable:
            method: dict[str, Any] = {
                "id": SHIPPING_METHOD_ID,
                "type": "shipping",
                "line_item_ids": shippable,
                "destinations": [],
                "groups": [
                    {
                        "id": GROUP_ID,
                        "line_item_ids": shippable,
                        "options": [
                            {
                                "id": o["id"],
                                "title": o["title"],
                                "totals": [{"type": "total", "amount": to_minor(o["amount"], cur)}],
                            }
                            for o in cart.shipping_options
                        ],
                        "selected_option_id": cart.shipping_rate_handle,
                    }
                ],
            }
            if a := cart.shipping_address:
                method["destinations"] = [
                    {
                        k: v
                        for k, v in {
                            "id": DESTINATION_ID,
                            "first_name": a.get("first_name"),
                            "last_name": a.get("last_name"),
                            "street_address": a.get("address1"),
                            "extended_address": a.get("address2"),
                            "address_locality": a.get("city"),
                            "address_region": a.get("province_code"),
                            "postal_code": a.get("zip"),
                            "address_country": a.get("country_code"),
                        }.items()
                        if v
                    }
                ]
                method["selected_destination_id"] = DESTINATION_ID
            body["fulfillment"] = {"methods": [method]}
        return body

    def render_error(self, error: ProtocolError) -> dict[str, Any]:
        """Render an error as a UCP error response."""
        out: dict[str, Any] = {
            "ucp": envelope("error"),
            "code": error.code,
            "content": error.message,
        }
        if error.param:
            out["path"] = error.param
        return out


def _title(line: dict[str, Any]) -> str | None:
    title = line.get("title")
    variant = line.get("variant_title")
    return f"{title} - {variant}" if title and variant else title


def _message(message: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    out = {
        "type": "error" if message["type"] == "error" else "warning",
        "code": message["code"],
        "content": message["content"],
        "severity": "recoverable",
    }
    if path := _path(message.get("path"), lines):
        out["path"] = path
    return out


def _path(path: str | None, lines: list[dict[str, Any]]) -> str | None:
    if not path:
        return None
    if path.startswith("line:"):
        lid = path.removeprefix("line:")
        index = next((i for i, li in enumerate(lines) if li["id"] == lid), None)
        return f"$.line_items[{index}]" if index is not None else "$.line_items"
    return {
        "lines": "$.line_items",
        "buyer.email": "$.buyer.email",
        "shipping_address": "$.fulfillment.methods[0].destinations",
        "shipping_option": "$.fulfillment.methods[0].groups[0].selected_option_id",
    }.get(path, "$." + path)
