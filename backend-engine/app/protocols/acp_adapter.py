"""Agentic Commerce Protocol (OpenAI) checkout adapter.

Implements the cart-building side of the Agentic Checkout spec:
https://developers.openai.com/commerce/specs/checkout. Amounts are integers in minor units and
`currency` is lowercase. Delegated payment (`complete`) is not offered: a ready session carries
`continue_url`, the merchant's own Shopify checkout, where the buyer pays.
"""

from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.models import AgentCart, AgentProtocol, CartStatus
from app.protocols.base import (
    EMAIL_PATTERN,
    ProtocolAdapter,
    ProtocolError,
    split_name,
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

API_VERSION = "2026-09-29"

STATUS = {
    CartStatus.OPEN: "not_ready_for_payment",
    CartStatus.READY: "ready_for_payment",
    CartStatus.COMPLETED: "completed",
    CartStatus.CANCELED: "canceled",
    CartStatus.EXPIRED: "canceled",
}

# ACP error codes are a closed set; map the cart's richer codes onto it.
MESSAGE_CODES = {"item_unavailable": "out_of_stock"}
ACP_CODES = {"missing", "invalid", "out_of_stock"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class AcpItem(_Model):
    id: str = Field(min_length=1, max_length=255)
    quantity: int = Field(ge=1, le=999)


class AcpBuyer(_Model):
    name: str | None = Field(default=None, max_length=256)
    first_name: str | None = Field(default=None, max_length=256)
    last_name: str | None = Field(default=None, max_length=256)
    email: str | None = Field(default=None, max_length=256, pattern=EMAIL_PATTERN)
    phone_number: str | None = Field(default=None, max_length=32)


class AcpAddress(_Model):
    name: str | None = Field(default=None, max_length=256)
    line_one: str = Field(min_length=1, max_length=60)
    line_two: str | None = Field(default=None, max_length=60)
    city: str = Field(min_length=1, max_length=60)
    state: str | None = Field(default=None, max_length=60)
    country: str = Field(min_length=2, max_length=2)
    postal_code: str | None = Field(default=None, max_length=20)
    phone_number: str | None = Field(default=None, max_length=32)


class CreateCheckoutSession(_Model):
    items: list[AcpItem] = Field(min_length=1, max_length=100)
    buyer: AcpBuyer | None = None
    fulfillment_address: AcpAddress | None = None


class UpdateCheckoutSession(_Model):
    items: list[AcpItem] | None = Field(default=None, min_length=1, max_length=100)
    buyer: AcpBuyer | None = None
    fulfillment_address: AcpAddress | None = None
    fulfillment_option_id: str | None = None


class AcpAdapter(ProtocolAdapter):
    protocol: ClassVar[AgentProtocol] = AgentProtocol.ACP
    create_model: ClassVar[type[BaseModel]] = CreateCheckoutSession
    update_model: ClassVar[type[BaseModel]] = UpdateCheckoutSession

    @staticmethod
    def _buyer(buyer: AcpBuyer | None) -> BuyerInput | None:
        if buyer is None:
            return None
        first, last = buyer.first_name, buyer.last_name
        if not first and not last:
            first, last = split_name(buyer.name)
        return BuyerInput(
            email=buyer.email, first_name=first, last_name=last, phone=buyer.phone_number
        )

    def _changes(self, request: BaseModel) -> CartChanges:
        assert isinstance(request, CreateCheckoutSession | UpdateCheckoutSession)
        buyer = self._buyer(request.buyer)
        address = None
        if a := request.fulfillment_address:
            first, last = split_name(a.name)
            address = AddressInput(
                first_name=first,
                last_name=last,
                address1=a.line_one,
                address2=a.line_two,
                city=a.city,
                province_code=a.state,
                country_code=a.country.upper(),
                zip=a.postal_code,
                phone=a.phone_number,
            )
        lines = None
        if request.items is not None:
            lines = [self._line(i, item) for i, item in enumerate(request.items)]
        return CartChanges(
            lines=lines,
            buyer=buyer,
            shipping_address=address,
            shipping_rate_handle=getattr(request, "fulfillment_option_id", None),
        )

    @staticmethod
    def _line(index: int, item: AcpItem) -> LineInput:
        try:
            return LineInput(variant_gid(item.id), item.quantity, ref=item.id)
        except CartError as exc:
            raise ProtocolError(400, "invalid", exc.message, f"$.items[{index}].id") from exc

    def render(self, cart: AgentCart) -> dict[str, Any]:
        cur = cart.currency
        lines = cart.lines

        def minor(value: Decimal | str) -> int:
            return to_minor(value, cur)

        line_items = [
            {
                "id": li["id"],
                "item": {"id": li.get("ref") or li["variant_id"].rsplit("/", 1)[-1],
                         "quantity": li["quantity"]},
                "base_amount": minor(Decimal(li["subtotal"]) + Decimal(li["discount"])),
                "discount": minor(li["discount"]),
                "subtotal": minor(li["subtotal"]),
                "tax": minor(li["tax"]),
                "total": minor(li["total"]),
            }
            for li in lines
        ]  # fmt: skip
        items_base = sum(i["base_amount"] for i in line_items)
        items_discount = sum(i["discount"] for i in line_items)
        totals = [
            {"type": "items_base_amount", "display_text": "Item(s) total", "amount": items_base},
            {"type": "items_discount", "display_text": "Discounts", "amount": items_discount},
            {"type": "subtotal", "display_text": "Subtotal", "amount": minor(cart.subtotal)},
            {"type": "fulfillment", "display_text": "Shipping", "amount": minor(cart.shipping)},
            {"type": "tax", "display_text": "Tax", "amount": minor(cart.tax)},
            {"type": "total", "display_text": "Total", "amount": minor(cart.total)},
        ]
        body: dict[str, Any] = {
            "id": str(cart.id),
            "status": STATUS[cart.status],
            "currency": cur.lower(),
            "line_items": line_items,
            "fulfillment_options": [
                {
                    "type": "shipping",
                    "id": o["id"],
                    "title": o["title"],
                    "subtitle": "",
                    "carrier": o["title"],
                    "subtotal": minor(o["amount"]),
                    "tax": 0,
                    "total": minor(o["amount"]),
                }
                for o in cart.shipping_options
            ],
            "totals": totals,
            "messages": [self._message(m, lines) for m in cart.messages],
            "links": [],
        }
        if cart.status is CartStatus.READY and cart.checkout_url:
            body["continue_url"] = cart.checkout_url
            body["messages"].append(
                {
                    "type": "info",
                    "param": "$.continue_url",
                    "content_type": "plain",
                    "content": "The buyer pays on the store's own checkout: send them to "
                    "continue_url.",
                }
            )
        if cart.order_gid:
            body["order"] = {
                "id": cart.order_gid,
                "checkout_session_id": str(cart.id),
                **({"permalink_url": cart.order_status_url} if cart.order_status_url else {}),
            }
        if cart.shipping_rate_handle:
            body["fulfillment_option_id"] = cart.shipping_rate_handle
        if cart.buyer:
            b = cart.buyer
            body["buyer"] = {
                k: v
                for k, v in {
                    "first_name": b.get("first_name"),
                    "last_name": b.get("last_name"),
                    "email": b.get("email"),
                    "phone_number": b.get("phone"),
                }.items()
                if v
            }
        if a := cart.shipping_address:
            name = " ".join(p for p in (a.get("first_name"), a.get("last_name")) if p)
            body["fulfillment_address"] = {
                k: v
                for k, v in {
                    "name": name or None,
                    "line_one": a.get("address1"),
                    "line_two": a.get("address2"),
                    "city": a.get("city"),
                    "state": a.get("province_code"),
                    "country": a.get("country_code"),
                    "postal_code": a.get("zip"),
                    "phone_number": a.get("phone"),
                }.items()
                if v
            }
        return body

    @staticmethod
    def _message(message: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
        param = _param(message.get("path"), lines)
        if message["type"] != "error":
            return {
                "type": "info",
                "param": param or "$",
                "content_type": "plain",
                "content": message["content"],
            }
        out = {
            "type": "error",
            "code": MESSAGE_CODES.get(message["code"], message["code"]),
            "content_type": "plain",
            "content": message["content"],
        }
        if out["code"] not in ACP_CODES:
            out["code"] = "invalid"
        if param:
            out["param"] = param
        return out

    def render_error(self, error: ProtocolError) -> dict[str, Any]:
        kind = {
            409: "invalid_request",
            503: "service_unavailable",
            502: "processing_error",
        }.get(error.status, "invalid_request" if error.status < 500 else "processing_error")
        out = {"type": kind, "code": error.code, "message": error.message}
        if error.param:
            out["param"] = error.param
        return out


def _param(path: str | None, lines: list[dict[str, Any]]) -> str | None:
    if not path:
        return None
    if path.startswith("line:"):
        lid = path.removeprefix("line:")
        index = next((i for i, li in enumerate(lines) if li["id"] == lid), None)
        return f"$.line_items[{index}]" if index is not None else "$.line_items"
    return {
        "lines": "$.line_items",
        "buyer.email": "$.buyer.email",
        "shipping_address": "$.fulfillment_address",
        "shipping_option": "$.fulfillment_option_id",
    }.get(path, "$." + path)
