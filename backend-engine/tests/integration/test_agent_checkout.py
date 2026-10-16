"""Phase 4: paid checkout over ACP, UCP, MCP and POST /agent-checkout (Shopify + Stripe faked)."""

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.conftest import sync_engine
from tests.fakes import FakeGateway, FakeShopify
from tests.integration.test_agent_protocols import (
    ADDRESS_ACP,
    agent_headers,
    issue,
    mcp_call,
    new_shop,
    tool,
)
from tests.integration.test_catalog import auth, install

READY_BODY = {
    "items": [{"id": "101", "quantity": 1}],
    "buyer": {"name": "Ada Lovelace", "email": "ada@example.com"},
    "fulfillment_address": ADDRESS_ACP,
}


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def client(app_env: None, gateway: FakeGateway) -> Iterator[TestClient]:
    from app.main import create_app

    app = create_app()
    app.state.payment_gateway = gateway
    with TestClient(app) as c:
        yield c


def acp_cart(client: TestClient, key: str, body: dict[str, Any] = READY_BODY) -> str:
    resp = client.post(
        "/api/v1/acp/checkout_sessions", json=body, headers=agent_headers(key, f"c-{id(body)}")
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def complete(client: TestClient, key: str, sid: str, token: str, idem: str) -> Any:
    return client.post(
        f"/api/v1/acp/checkout_sessions/{sid}/complete",
        json={"payment_data": {"token": token, "provider": "stripe"}},
        headers=agent_headers(key, idem),
    )


def transactions(sid: str) -> list[Any]:
    from app.models import AgentTransaction

    with Session(sync_engine()) as s:
        return list(
            s.exec(select(AgentTransaction).where(AgentTransaction.cart_id == sid)).all()  # type: ignore[arg-type]
        )


def events(txn_id: Any) -> list[Any]:
    from app.models import TransactionEvent

    with Session(sync_engine()) as s:
        rows = s.exec(
            select(TransactionEvent)
            .where(TransactionEvent.transaction_id == txn_id)
            .order_by(TransactionEvent.id)  # type: ignore[arg-type]
        ).all()
        return [(e.event_type, e.payload) for e in rows]


@pytest.fixture
def acp(client: TestClient) -> tuple[str, str]:
    shop = new_shop()
    install(client, shop)
    return shop, issue(client, shop, "acp")["api_key"]


def test_acp_complete_creates_paid_order(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway, acp: tuple[str, str]
) -> None:
    shop, key = acp
    sid = acp_cart(client, key)

    resp = complete(client, key, sid, "spt_ok", "pay-1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["order"] == {
        "id": "gid://shopify/Order/1001",
        "checkout_session_id": sid,
        "permalink_url": "https://acme.example/orders/1001/status",
    }

    # Charged exactly the Shopify total, in cents, never with the raw token persisted.
    (charge,) = gateway.charges
    assert charge["amount_minor"] == 13750 and charge["currency"] == "USD"
    assert charge["token"] == "spt_ok"
    draft = next(iter(shopify.drafts.values()))
    assert "Origin: Agentic Commerce" in draft["tags"]
    assert draft["shippingLine"] == {"shippingRateHandle": "shopify-Standard-5.00"}
    assert draft["email"] == "ada@example.com"

    (txn,) = transactions(sid)
    assert txn.status.value == "order_created"
    assert (txn.order_name, txn.payment_ref, txn.amount) == ("#1001", "pi_1", Decimal("137.50"))
    trail = events(txn.id)
    assert [e for e, _ in trail] == [
        "checkout_started",
        "draft_order_created",
        "payment_captured",
        "order_created",
    ]
    assert "spt_ok" not in json.dumps([p for _, p in trail]) + str(txn.model_dump())

    # Replays are safe; a second payment is refused.
    again = complete(client, key, sid, "spt_ok", "pay-1")
    assert again.json() == body and len(gateway.charges) == 1
    other = complete(client, key, sid, "spt_ok", "pay-2")
    assert other.status_code == 409 and other.json()["code"] == "already_completed"

    # The merchant sees it in the order log.
    page = client.get("/api/v1/merchant/transactions", headers=auth(shop)).json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["order_name"] == "#1001" and item["agent_name"] == "acp agent"
    assert item["admin_url"].endswith("/orders/1001")


def test_declined_payment_then_retry(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway, acp: tuple[str, str]
) -> None:
    _, key = acp
    sid = acp_cart(client, key)

    resp = complete(client, key, sid, "spt_decline", "pay-1")
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "ready_for_payment"
    assert body["messages"] == [
        {
            "type": "error",
            "code": "payment_declined",
            "content_type": "plain",
            "content": "Your card was declined.",
            "param": "$.payment_data",
        }
    ]
    assert shopify.deleted == list(shopify.drafts)  # the unpaid draft is removed
    (failed,) = transactions(sid)
    assert (failed.status.value, failed.error_code) == ("failed", "payment_declined")

    ok = complete(client, key, sid, "spt_ok", "pay-2")
    assert ok.json()["status"] == "completed" and ok.json()["messages"] == []


def test_cart_not_ready_is_not_charged(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway, acp: tuple[str, str]
) -> None:
    _, key = acp
    sid = acp_cart(client, key, {"items": [{"id": "101", "quantity": 1}]})
    body = complete(client, key, sid, "spt_ok", "pay-1").json()
    assert body["status"] == "not_ready_for_payment"
    assert {m["param"] for m in body["messages"]} == {"$.buyer.email", "$.fulfillment_address"}
    assert gateway.charges == [] and shopify.drafts == {}
    assert transactions(sid)[0].error_code == "not_ready"


def test_buyer_on_complete_makes_cart_ready(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway, acp: tuple[str, str]
) -> None:
    _, key = acp
    sid = acp_cart(client, key, {**READY_BODY, "buyer": None})
    resp = client.post(
        f"/api/v1/acp/checkout_sessions/{sid}/complete",
        json={
            "buyer": {"name": "Ada Lovelace", "email": "ada@example.com"},
            "payment_data": {"token": "spt_ok", "provider": "stripe"},
        },
        headers=agent_headers(key, "pay-1"),
    )
    assert resp.json()["status"] == "completed"


def test_order_failure_refunds(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway, acp: tuple[str, str]
) -> None:
    _, key = acp
    sid = acp_cart(client, key)
    shopify.fail_complete = True
    body = complete(client, key, sid, "spt_ok", "pay-1").json()
    assert body["status"] == "ready_for_payment"
    assert body["messages"][0]["content"].endswith("the payment was refunded.")
    assert gateway.refunds == ["pi_1"]
    (txn,) = transactions(sid)
    assert (txn.status.value, txn.error_code) == ("refunded", "order_failed")
    assert [e for e, _ in events(txn.id)][-2:] == ["payment_refunded", "checkout_failed"]


def test_price_change_aborts_before_charge(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway, acp: tuple[str, str]
) -> None:
    _, key = acp
    sid = acp_cart(client, key)
    shopify.price_skew = Decimal("1.00")
    body = complete(client, key, sid, "spt_ok", "pay-1").json()
    assert body["messages"][0]["code"] == "invalid"
    assert "total changed" in body["messages"][0]["content"]
    assert gateway.charges == []
    assert transactions(sid)[0].error_code == "price_changed"


def test_ucp_complete_with_payment_instrument(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway
) -> None:
    shop = new_shop()
    install(client, shop)
    key = issue(client, shop, "ucp")["api_key"]
    created = client.post(
        "/api/v1/ucp/checkout-sessions",
        json={
            "line_items": [{"item": {"id": "101"}, "quantity": 1}],
            "buyer": {"email": "ada@example.com"},
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
                    }
                ]
            },
        },
        headers=agent_headers(key, "u1"),
    ).json()
    assert created["payment"]["handlers"][0]["id"] == "stripe"

    payment = {
        "instruments": [
            {
                "id": "pi",
                "handler_id": "stripe",
                "type": "card",
                "credential": {"type": "shared_payment_token", "token": "spt_ok"},
            }
        ]
    }
    resp = client.post(
        f"/api/v1/ucp/checkout-sessions/{created['id']}/complete",
        json={"payment": payment},
        headers=agent_headers(key, "u2"),
    )
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "completed"
    assert body["order"]["label"] == "#1001"

    bad = {"instruments": [{"handler_id": "com.example.other", "credential": {"token": "x"}}]}
    resp = client.post(
        f"/api/v1/ucp/checkout-sessions/{created['id']}/complete",
        json={"payment": bad},
        headers=agent_headers(key, "u3"),
    )
    assert resp.status_code == 400 and resp.json()["path"] == "$.payment.instruments"

    profile = client.get("/.well-known/ucp").json()["ucp"]
    assert "com.stripe.shared_payment_token" in profile["payment_handlers"]


def test_generic_agent_checkout(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway, acp: tuple[str, str]
) -> None:
    shop, key = acp
    sid = acp_cart(client, key)

    resp = client.post(
        "/api/v1/agent-checkout", json={"checkout_id": sid}, headers=agent_headers(key, "g0")
    )
    assert resp.status_code == 400

    # Another agent of the same store cannot pay for this cart.
    other_key = issue(client, shop, "ucp")["api_key"]
    payment = {"checkout_id": sid, "payment_data": {"token": "spt_ok", "provider": "stripe"}}
    resp = client.post(
        "/api/v1/agent-checkout", json=payment, headers=agent_headers(other_key, "g1")
    )
    assert resp.status_code == 404

    resp = client.post("/api/v1/agent-checkout", json=payment, headers=agent_headers(key, "g2"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "order_created" and body["amount"] == 13750
    assert body["order"]["name"] == "#1001" and body["error"] is None


def test_generic_agent_checkout_decline_is_402(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway, acp: tuple[str, str]
) -> None:
    _, key = acp
    sid = acp_cart(client, key)
    payment = {
        "checkout_id": sid,
        "payment": {"instruments": [{"credential": {"token": "spt_3ds"}}]},
    }
    resp = client.post("/api/v1/agent-checkout", json=payment, headers=agent_headers(key, "g1"))
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "requires_3ds"


def test_mcp_checkout_tool(client: TestClient, shopify: FakeShopify, gateway: FakeGateway) -> None:
    shop = new_shop()
    install(client, shop)
    key = issue(client, shop, "mcp")["api_key"]
    cart = tool(
        client,
        key,
        "create_cart",
        lines=[{"variant_id": "101", "quantity": 1}],
        email="ada@example.com",
        shipping_address={
            "address1": "1 Main St",
            "city": "Austin",
            "country_code": "US",
            "zip": "78701",
        },
    )
    paid = tool(client, key, "checkout", cart_id=cart["cart_id"], payment_token="spt_ok")
    assert paid["status"] == "completed"
    assert paid["payment"]["order"]["name"] == "#1001"
    # Same token again: replayed, not charged twice.
    again = tool(client, key, "checkout", cart_id=cart["cart_id"], payment_token="spt_ok")
    assert again["payment"]["status"] == "order_created" and len(gateway.charges) == 1
    assert mcp_call(client, key, "tools/list").status_code == 200


def test_payments_not_configured(app_env: None, shopify: FakeShopify) -> None:
    from app.main import create_app

    with TestClient(create_app()) as c:  # no fake gateway, no STRIPE_SECRET_KEY
        shop = new_shop()
        install(c, shop)
        key = issue(c, shop, "acp")["api_key"]
        sid = acp_cart(c, key)
        resp = complete(c, key, sid, "spt_ok", "p1")
        assert resp.status_code == 503 and resp.json()["code"] == "payments_not_configured"
        assert transactions(sid) == []

        settings = c.get("/api/v1/merchant/payments", headers=auth(shop)).json()
        assert settings == {
            "stripe_account_id": None,
            "stripe_configured": False,
            "test_mode": False,
        }
        resp = c.put(
            "/api/v1/merchant/payments", json={"stripe_account_id": "acct_123"}, headers=auth(shop)
        )
        assert resp.json()["stripe_account_id"] == "acct_123"
        bad = c.put(
            "/api/v1/merchant/payments", json={"stripe_account_id": "nope"}, headers=auth(shop)
        )
        assert bad.status_code == 422


def test_unexpected_gateway_error_does_not_block_the_cart(
    client: TestClient, shopify: FakeShopify, gateway: FakeGateway, acp: tuple[str, str]
) -> None:
    _, key = acp
    sid = acp_cart(client, key)
    original = gateway.charge

    async def boom(**kwargs: Any) -> Any:
        raise RuntimeError("network down")

    gateway.charge = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        complete(client, key, sid, "spt_ok", "pay-1")
    assert transactions(sid)[0].status.value == "failed"

    gateway.charge = original  # type: ignore[method-assign]
    assert complete(client, key, sid, "spt_ok", "pay-2").json()["status"] == "completed"
