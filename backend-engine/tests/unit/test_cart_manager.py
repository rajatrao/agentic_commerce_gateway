import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.models import AgentCart, AgentProtocol, CartStatus
from app.models.base import utcnow
from app.services.cart_manager import (
    AddressInput,
    BuyerInput,
    CartChanges,
    CartError,
    CartManager,
    LineInput,
    variant_gid,
)
from tests.fakes import FakeShopify, V

MERCHANT = uuid.uuid4()
ADDRESS = AddressInput(
    address1="1 Main St", city="Austin", province_code="TX", country_code="US", zip="78701"
)
BUYER = BuyerInput(email="ada@example.com")


class MemoryCarts:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, AgentCart] = {}

    def get(self, merchant_id: uuid.UUID, cart_id: uuid.UUID) -> AgentCart | None:
        cart = self.rows.get(cart_id)
        return cart if cart and cart.merchant_id == merchant_id else None

    def save(self, cart: AgentCart) -> AgentCart:
        self.rows[cart.id] = cart
        return cart


@pytest.fixture
def shopify() -> FakeShopify:
    return FakeShopify()


@pytest.fixture
def manager(shopify: FakeShopify) -> CartManager:
    return CartManager(MemoryCarts(), shopify)  # type: ignore[arg-type]


def lines(*pairs: tuple[int, int]) -> list[LineInput]:
    return [LineInput(f"{V}{vid}", qty) for vid, qty in pairs]


async def create(manager: CartManager, changes: CartChanges) -> AgentCart:
    return await manager.create(MERCHANT, AgentProtocol.ACP, changes)


def messages(cart: AgentCart) -> dict[str, str]:
    return {m["path"]: m["code"] for m in cart.messages}


async def test_complete_cart_is_priced_by_shopify_and_ready(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((101, 2)), BUYER, ADDRESS))
    assert cart.status is CartStatus.READY
    assert cart.messages == []
    # Cheapest rate is preselected; tax is 10% of items + shipping.
    assert cart.shipping_rate_handle == "shopify-Standard-5.00"
    assert [o["id"] for o in cart.shipping_options] == [
        "shopify-Express-15.00",
        "shopify-Standard-5.00",
    ]
    assert (cart.subtotal, cart.shipping, cart.tax, cart.total) == (
        Decimal("240.00"),
        Decimal("5.00"),
        Decimal("24.50"),
        Decimal("269.50"),
    )
    line = cart.lines[0]
    assert line["id"] == "li_101"
    assert line["title"] == "Trail Jacket" and line["variant_title"] == "M"
    assert (line["unit_price"], line["subtotal"], line["tax"], line["total"]) == (
        "120.00",
        "240.00",
        "24.50",
        "264.50",
    )
    assert cart.expires_at is not None and cart.expires_at > utcnow()


async def test_missing_buyer_and_address_keep_cart_open(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((101, 1))))
    assert cart.status is CartStatus.OPEN
    assert messages(cart) == {"buyer.email": "missing", "shipping_address": "missing"}
    assert cart.subtotal == Decimal("120.00") and cart.shipping == Decimal("0")


async def test_digital_only_cart_needs_no_address(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((401, 3)), BUYER))
    assert cart.status is CartStatus.READY
    assert cart.shipping_rate_handle is None
    assert cart.total == Decimal("33.00")


async def test_stock_rules(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((102, 5), (201, 1), (301, 1)), BUYER, ADDRESS))
    by_id = {li["id"]: li for li in cart.lines}
    # Over-request is clamped to stock with a warning, and still priced.
    assert by_id["li_102"]["quantity"] == 1 and by_id["li_102"]["requested_quantity"] == 5
    assert by_id["li_201"]["available"] is False  # tracked, deny, zero stock
    assert by_id["li_301"]["available"] is False  # archived product
    assert messages(cart) == {
        "line:li_102": "quantity_adjusted",
        "line:li_201": "out_of_stock",
        "line:li_301": "item_unavailable",
    }
    assert cart.status is CartStatus.OPEN
    assert cart.subtotal == Decimal("120.00")


async def test_untracked_or_continue_policy_ignores_stock(
    manager: CartManager, shopify: FakeShopify
) -> None:
    shopify.variants[f"{V}102"]["inventoryPolicy"] = "CONTINUE"
    cart = await create(manager, CartChanges(lines((102, 5)), BUYER, ADDRESS))
    assert cart.lines[0]["quantity"] == 5 and cart.status is CartStatus.READY


async def test_unknown_variant_is_unavailable(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((999, 1)), BUYER, ADDRESS))
    assert messages(cart)["line:li_999"] == "item_unavailable"
    assert messages(cart)["lines"] == "missing"


async def test_update_selects_rate_and_rejects_unknown_one(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((101, 1)), BUYER, ADDRESS))
    cart = await manager.update(cart, CartChanges(shipping_rate_handle="shopify-Express-15.00"))
    assert cart.shipping == Decimal("15.00") and cart.status is CartStatus.READY

    cart = await manager.update(cart, CartChanges(shipping_rate_handle="bogus"))
    assert messages(cart) == {"shipping_option": "invalid"}
    assert cart.shipping_rate_handle == "shopify-Standard-5.00"
    assert cart.status is CartStatus.OPEN


async def test_new_address_resets_rate(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((101, 1)), BUYER, ADDRESS))
    await manager.update(cart, CartChanges(shipping_rate_handle="shopify-Express-15.00"))
    moved = AddressInput(address1="9 Elm", city="Reno", country_code="US", zip="89501")
    cart = await manager.update(cart, CartChanges(shipping_address=moved))
    assert cart.shipping_rate_handle == "shopify-Standard-5.00"


async def test_unshippable_destination(manager: CartManager) -> None:
    nowhere = AddressInput(address1="1 Road", city="Nowhere", country_code="ZZ")
    cart = await create(manager, CartChanges(lines((101, 1)), BUYER, nowhere))
    assert messages(cart) == {"shipping_address": "invalid"}
    assert cart.shipping_options == []


async def test_lines_merge_and_validate(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((101, 1), (101, 2)), BUYER, ADDRESS))
    assert [(li["id"], li["quantity"]) for li in cart.lines] == [("li_101", 3)]
    with pytest.raises(CartError):
        await create(manager, CartChanges(lines((101, 0))))
    with pytest.raises(CartError):
        await create(manager, CartChanges([]))


async def test_tax_is_spread_over_lines(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((101, 1), (401, 1)), BUYER, ADDRESS))
    assert sum(Decimal(li["tax"]) for li in cart.lines) == cart.tax
    assert sum(Decimal(li["total"]) for li in cart.lines) + cart.shipping == cart.total


async def test_cancel_and_expiry(manager: CartManager) -> None:
    cart = await create(manager, CartChanges(lines((101, 1)), BUYER, ADDRESS))
    manager.cancel(cart)
    with pytest.raises(CartError) as exc:
        await manager.update(cart, CartChanges(buyer=BUYER))
    assert exc.value.code == "not_modifiable"

    other = await create(manager, CartChanges(lines((101, 1))))
    other.expires_at = utcnow() - timedelta(seconds=1)
    assert manager.get(MERCHANT, other.id).status is CartStatus.EXPIRED
    with pytest.raises(CartError):
        manager.get(uuid.uuid4(), other.id)


def test_variant_gid_accepts_ids_and_gids() -> None:
    assert variant_gid(123) == f"{V}123"
    assert variant_gid(f"{V}123") == f"{V}123"
    with pytest.raises(CartError):
        variant_gid("sku-123")
