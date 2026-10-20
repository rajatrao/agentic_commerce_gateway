"""Orders placed from an agent's checkout link are linked back to the cart by webhook."""

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import WEBHOOK_SECRET, sync_engine
from tests.fakes import FakeShopify
from tests.integration.test_agent_protocols import agent_headers, issue, new_shop
from tests.integration.test_catalog import auth, install


@pytest.fixture
def client(app_env: None) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def webhook(client: TestClient, shop: str, topic: str, body: dict[str, Any]) -> str:
    raw = json.dumps(body).encode()
    sig = base64.b64encode(hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest())
    webhook_id = str(uuid.uuid4())
    resp = client.post(
        "/api/v1/webhooks/shopify",
        content=raw,
        headers={
            "X-Shopify-Hmac-Sha256": sig.decode(),
            "X-Shopify-Topic": topic,
            "X-Shopify-Shop-Domain": shop,
            "X-Shopify-Webhook-Id": webhook_id,
            "Content-Type": "application/json",
        },
    )
    assert resp.json() == {"status": "ok"}, resp.text
    return webhook_id


def order(order_id: int, cart_id: str | None, email: str = "ada@example.com") -> dict[str, Any]:
    attrs = [{"name": "gift", "value": "no"}]
    if cart_id:
        attrs.append({"name": "_agentic_cart_id", "value": cart_id})
    return {
        "id": order_id,
        "admin_graphql_api_id": f"gid://shopify/Order/{order_id}",
        "name": f"#{order_id}",
        "email": email,
        "order_status_url": f"https://shop.example/orders/{order_id}/authenticate?key=k",
        "customer": {"email": email, "first_name": "Ada"},
        "shipping_address": {"address1": "1 Main St"},
        "note_attributes": attrs,
    }


def acp_cart(client: TestClient, key: str, idem: str, **extra: Any) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/acp/checkout_sessions",
        json={"items": [{"id": "101", "quantity": 1}], **extra},
        headers=agent_headers(key, idem),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_order_webhook_completes_the_cart(client: TestClient, shopify: FakeShopify) -> None:
    shop = new_shop()
    install(client, shop)
    key = issue(client, shop, "acp")["api_key"]
    cart = acp_cart(client, key, "o-1")
    assert cart["status"] == "ready_for_payment" and cart["continue_url"]

    hook_id = webhook(client, shop, "orders/create", order(5001, cart["id"]))

    got = client.get(f"/api/v1/acp/checkout_sessions/{cart['id']}", headers=agent_headers(key))
    body = got.json()
    assert body["status"] == "completed"
    assert "continue_url" not in body
    assert body["order"] == {
        "id": "gid://shopify/Order/5001",
        "checkout_session_id": cart["id"],
        "permalink_url": "https://shop.example/orders/5001/authenticate?key=k",
    }
    # A completed cart can't be changed any more.
    upd = client.post(
        f"/api/v1/acp/checkout_sessions/{cart['id']}",
        json={"items": [{"id": "101", "quantity": 2}]},
        headers=agent_headers(key, "o-1-upd"),
    )
    assert upd.status_code == 405

    page = client.get("/api/v1/merchant/agent-checkouts", headers=auth(shop)).json()
    assert page["ordered"] == 1
    item = page["items"][0]
    assert (item["status"], item["order_name"]) == ("completed", "#5001")
    store = shop.removesuffix(".myshopify.com")
    assert item["order_admin_url"] == f"https://admin.shopify.com/store/{store}/orders/5001"
    assert item["ordered_at"]

    # Only identifiers from the order payload are stored, never customer data.
    with sync_engine().connect() as conn:
        stored = conn.execute(
            text("SELECT payload FROM webhook_events WHERE webhook_id = :id"), {"id": hook_id}
        ).scalar_one()
    assert stored == {
        "id": 5001,
        "admin_graphql_api_id": "gid://shopify/Order/5001",
        "name": "#5001",
        "agentic_cart_id": cart["id"],
    }


def test_only_agent_orders_for_this_store_are_linked(
    client: TestClient, shopify: FakeShopify
) -> None:
    shop, other = new_shop(), new_shop()
    install(client, shop)
    install(client, other)
    key = issue(client, shop, "acp")["api_key"]
    cart = acp_cart(client, key, "o-2")

    webhook(client, shop, "orders/create", order(6001, None))  # a regular storefront order
    webhook(client, shop, "orders/create", order(6002, "not-a-uuid"))
    webhook(client, other, "orders/create", order(6003, cart["id"]))  # wrong store
    page = client.get("/api/v1/merchant/agent-checkouts", headers=auth(shop)).json()
    assert page["ordered"] == 0 and page["items"][0]["status"] == "ready"

    webhook(client, shop, "orders/create", order(6004, cart["id"]))
    webhook(client, shop, "orders/create", order(6005, cart["id"]))  # link reused: first wins
    item = client.get("/api/v1/merchant/agent-checkouts", headers=auth(shop)).json()["items"][0]
    assert item["order_name"] == "#6004"


def test_ucp_shows_the_order(client: TestClient, shopify: FakeShopify) -> None:
    shop = new_shop()
    install(client, shop)
    ucp_key = issue(client, shop, "ucp")["api_key"]
    created = client.post(
        "/api/v1/ucp/checkout-sessions",
        json={"line_items": [{"item": {"id": "101"}, "quantity": 1}]},
        headers=agent_headers(ucp_key, "u-1"),
    )
    assert created.status_code == 201, created.text
    cart_id = created.json()["id"]
    webhook(client, shop, "orders/create", order(7001, cart_id))
    body = client.get(
        f"/api/v1/ucp/checkout-sessions/{cart_id}", headers=agent_headers(ucp_key)
    ).json()
    assert body["status"] == "completed"
    assert body["order"]["id"] == "gid://shopify/Order/7001"
    assert "continue_url" not in body


def test_customer_redact_clears_buyer_details(client: TestClient, shopify: FakeShopify) -> None:
    shop = new_shop()
    install(client, shop)
    key = issue(client, shop, "acp")["api_key"]
    buyer = {"buyer": {"first_name": "Ada", "email": "Ada@Example.com"}}
    ordered = acp_cart(client, key, "r-1", **buyer)
    live = acp_cart(client, key, "r-2", **buyer)
    untouched = acp_cart(client, key, "r-3", buyer={"first_name": "Bo", "email": "bo@x.com"})
    webhook(client, shop, "orders/create", order(8001, ordered["id"]))

    webhook(
        client,
        shop,
        "customers/redact",
        {"customer": {"id": 1, "email": "ada@example.com"}, "orders_to_redact": [8001]},
    )

    def get(cart_id: str) -> dict[str, Any]:
        resp = client.get(f"/api/v1/acp/checkout_sessions/{cart_id}", headers=agent_headers(key))
        return resp.json()  # type: ignore[no-any-return]

    done = get(ordered["id"])
    assert "buyer" not in done and done["status"] == "completed"
    assert "permalink_url" not in done["order"]
    closed = get(live["id"])
    assert "buyer" not in closed and closed["status"] == "canceled"
    assert "continue_url" not in closed
    assert get(untouched["id"])["buyer"]["email"] == "bo@x.com"
