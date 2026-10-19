import uuid

import pytest

from app.models import AgentProtocol
from app.protocols.acp_adapter import AcpAdapter
from app.protocols.base import ProtocolError, to_minor
from app.protocols.ucp_adapter import UcpAdapter
from app.services.agent_keys import KeyVerifier, issue_key, key_prefix
from app.services.cart_manager import AddressInput, BuyerInput, CartChanges, CartManager, LineInput
from tests.fakes import FakeShopify, V
from tests.unit.test_cart_manager import MemoryCarts

ADDRESS = AddressInput(
    address1="1 Main St", city="Austin", province_code="TX", country_code="US", zip="78701"
)


async def priced_cart(protocol: AgentProtocol, *pairs: tuple[int, int], ready: bool = True):
    manager = CartManager(MemoryCarts(), FakeShopify(), "acme.myshopify.com")  # type: ignore[arg-type]
    changes = CartChanges(
        [LineInput(f"{V}{v}", q, ref=str(v)) for v, q in pairs],
        BuyerInput(email="ada@example.com", first_name="Ada", last_name="Lovelace")
        if ready
        else None,
        ADDRESS if ready else None,
    )
    return await manager.create(uuid.uuid4(), protocol, changes)


def test_to_minor() -> None:
    assert to_minor("12.34", "USD") == 1234
    assert to_minor("1200", "JPY") == 1200


# ----------------------------------------------------------------------------- ACP


def test_acp_parse_create() -> None:
    changes = AcpAdapter().parse_create(
        {
            "items": [{"id": "101", "quantity": 2}, {"id": f"{V}401", "quantity": 1}],
            "buyer": {"name": "Ada Lovelace", "email": "ada@example.com"},
            "fulfillment_address": {
                "name": "Ada Lovelace",
                "line_one": "1 Main St",
                "city": "Austin",
                "state": "TX",
                "country": "us",
                "postal_code": "78701",
            },
        }
    )
    assert changes.lines == [LineInput(f"{V}101", 2, "101"), LineInput(f"{V}401", 1, f"{V}401")]
    assert changes.buyer and changes.buyer.first_name == "Ada"
    assert changes.buyer.last_name == "Lovelace"
    assert changes.shipping_address and changes.shipping_address.country_code == "US"
    assert changes.shipping_address.address1 == "1 Main St"


@pytest.mark.parametrize(
    ("body", "param"),
    [
        ({"items": []}, "$.items"),
        ({"items": [{"id": "101", "quantity": 0}]}, "$.items.0.quantity"),
        ({"items": [{"id": "sku-1", "quantity": 1}]}, "$.items[0].id"),
        ({"items": [{"id": "1", "quantity": 1}], "buyer": {"email": "nope"}}, "$.buyer.email"),
    ],
)
def test_acp_rejects_bad_requests(body: dict, param: str) -> None:
    with pytest.raises(ProtocolError) as exc:
        AcpAdapter().parse_create(body)
    assert exc.value.status == 400 and exc.value.param == param


def test_acp_update_carries_fulfillment_option() -> None:
    changes = AcpAdapter().parse_update({"fulfillment_option_id": "shopify-Express-15.00"})
    assert changes == CartChanges(shipping_rate_handle="shopify-Express-15.00")


async def test_acp_render() -> None:
    cart = await priced_cart(AgentProtocol.ACP, (101, 2))
    body = AcpAdapter().render(cart)
    assert body["status"] == "ready_for_payment"
    assert body["currency"] == "usd"
    assert body["line_items"] == [
        {
            "id": "li_101",
            "item": {"id": "101", "quantity": 2},
            "base_amount": 24000,
            "discount": 0,
            "subtotal": 24000,
            "tax": 2450,
            "total": 26450,
        }
    ]
    totals = {t["type"]: t["amount"] for t in body["totals"]}
    assert totals == {
        "items_base_amount": 24000,
        "items_discount": 0,
        "subtotal": 24000,
        "fulfillment": 500,
        "tax": 2450,
        "total": 26950,
    }
    assert body["fulfillment_option_id"] == "shopify-Standard-5.00"
    assert {o["id"]: o["total"] for o in body["fulfillment_options"]} == {
        "shopify-Express-15.00": 1500,
        "shopify-Standard-5.00": 500,
    }
    assert body["buyer"] == {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
    }
    assert body["fulfillment_address"]["line_one"] == "1 Main St"
    assert body["continue_url"].startswith("https://acme.myshopify.com/cart/101:2?")
    assert [m["param"] for m in body["messages"]] == ["$.continue_url"]
    assert "payment_provider" not in body


async def test_acp_render_messages() -> None:
    cart = await priced_cart(AgentProtocol.ACP, (101, 1), (201, 1), ready=False)
    body = AcpAdapter().render(cart)
    assert body["status"] == "not_ready_for_payment"
    assert "continue_url" not in body
    by_param = {m.get("param"): (m["type"], m.get("code")) for m in body["messages"]}
    assert by_param == {
        "$.line_items[1]": ("error", "out_of_stock"),
        # Email and address only prefill the store's checkout, so they are not required.
        "$.buyer.email": ("info", None),
        "$.fulfillment_address": ("info", None),
    }


def test_acp_error_shape() -> None:
    err = ProtocolError(409, "request_not_idempotent", "reused key")
    assert AcpAdapter().render_error(err) == {
        "type": "invalid_request",
        "code": "request_not_idempotent",
        "message": "reused key",
    }


# ----------------------------------------------------------------------------- UCP


def test_ucp_parse_create_with_fulfillment() -> None:
    changes = UcpAdapter().parse_create(
        {
            "line_items": [{"item": {"id": "101"}, "quantity": 1}],
            "buyer": {"email": "ada@example.com", "first_name": "Ada"},
            "fulfillment": {
                "methods": [
                    {
                        "type": "shipping",
                        "destinations": [
                            {
                                "street_address": "1 Main St",
                                "address_locality": "Austin",
                                "address_region": "TX",
                                "postal_code": "78701",
                                "address_country": "US",
                            }
                        ],
                        "groups": [{"selected_option_id": "shopify-Express-15.00"}],
                    }
                ]
            },
        }
    )
    assert changes.lines == [LineInput(f"{V}101", 1, "101")]
    assert changes.shipping_address and changes.shipping_address.city == "Austin"
    assert changes.shipping_rate_handle == "shopify-Express-15.00"


async def test_ucp_render() -> None:
    cart = await priced_cart(AgentProtocol.UCP, (101, 1), (401, 1))
    body = UcpAdapter().render(cart)
    assert body["ucp"]["version"]
    assert "dev.ucp.shopping.checkout" in body["ucp"]["capabilities"]
    # Payment happens on the store's own checkout, so a ready cart escalates to the buyer.
    assert body["status"] == "requires_escalation"
    assert body["continue_url"].startswith("https://acme.myshopify.com/cart/101:1,401:1?")
    assert body["messages"][-1]["severity"] == "requires_buyer_review"
    assert "payment" not in body
    assert body["currency"] == "USD"
    assert body["line_items"][0]["item"] == {
        "id": "101",
        "title": "Trail Jacket - M",
        "price": 12000,
        "image_url": "https://cdn.example/101.jpg",
    }
    totals = {t["type"]: t["amount"] for t in body["totals"]}
    assert totals == {"subtotal": 13000, "fulfillment": 500, "tax": 1350, "total": 14850}
    method = body["fulfillment"]["methods"][0]
    assert method["line_item_ids"] == ["li_101"]  # the gift card does not ship
    assert method["selected_destination_id"] == "dest_1"
    assert method["groups"][0]["selected_option_id"] == "shopify-Standard-5.00"


async def test_ucp_render_messages() -> None:
    cart = await priced_cart(AgentProtocol.UCP, (102, 3), ready=False)
    messages = UcpAdapter().render(cart)["messages"]
    assert {(m["type"], m["code"], m.get("path")) for m in messages} == {
        ("warning", "quantity_adjusted", "$.line_items[0]"),
        ("warning", "missing", "$.buyer.email"),
        ("warning", "missing", "$.fulfillment.methods[0].destinations"),
        ("error", "requires_buyer_review", None),
    }


# ----------------------------------------------------------------------------- keys


def test_agent_keys() -> None:
    key = issue_key()
    assert key.plaintext.startswith("acg_live_")
    assert key_prefix(key.plaintext) == key.prefix
    assert key_prefix("acg_live_short") is None
    verifier = KeyVerifier()
    assert verifier.verify(key.plaintext, key.hash)
    assert verifier.verify(key.plaintext, key.hash)  # cached path
    assert not verifier.verify(key.plaintext + "x", key.hash)
