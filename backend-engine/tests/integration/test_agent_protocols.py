"""UCP, ACP and MCP end to end: agent keys, carts priced through (mocked) Shopify, idempotency."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import FRONTEND_URL
from tests.fakes import FakeShopify
from tests.integration.test_catalog import GRAPHQL_PATH, auth, install

ADDRESS_ACP = {
    "name": "Ada Lovelace",
    "line_one": "1 Main St",
    "city": "Austin",
    "state": "TX",
    "country": "US",
    "postal_code": "78701",
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


@pytest.fixture
def client(app_env: None) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def shopify() -> Iterator[FakeShopify]:
    fake = FakeShopify()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"data": fake.handle(body["query"], body["variables"])})

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{FRONTEND_URL}/internal/admin-token").respond(
            json={"access_token": "shpat_fresh", "expires_at": None}
        )
        router.post(url__regex=rf"https://[^/]+{GRAPHQL_PATH}").mock(side_effect=handler)
        router.route(host="testserver").pass_through()
        yield fake


def new_shop() -> str:
    return f"agents-{uuid.uuid4().hex[:8]}.myshopify.com"


def issue(client: TestClient, shop: str, protocol: str, **extra: Any) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/merchant/agent-clients",
        json={"name": f"{protocol} agent", "protocol": protocol, **extra},
        headers=auth(shop),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def agent_headers(key: str, idem: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}", "Request-Id": "req-1"}
    if idem:
        headers["Idempotency-Key"] = idem
    return headers


# ----------------------------------------------------------------------------- key management


def test_agent_client_management(client: TestClient) -> None:
    shop = new_shop()
    install(client, shop)
    created = issue(client, shop, "acp")
    assert created["api_key"].startswith(f"acg_live_{created['key_prefix']}_")
    assert created["scopes"] == ["catalog:read", "checkout"]

    listed = client.get("/api/v1/merchant/agent-clients", headers=auth(shop)).json()
    assert [c["id"] for c in listed] == [created["id"]]
    assert "api_key" not in listed[0]

    read_only = auth(shop, ["merchant:read"])
    assert (
        client.post(
            "/api/v1/merchant/agent-clients",
            json={"name": "x", "protocol": "ucp"},
            headers=read_only,
        ).status_code
        == 403
    )
    resp = client.delete(f"/api/v1/merchant/agent-clients/{created['id']}", headers=auth(shop))
    assert resp.status_code == 204
    listed = client.get("/api/v1/merchant/agent-clients", headers=auth(shop)).json()
    assert listed[0]["revoked_at"] is not None

    other = new_shop()
    install(client, other)
    resp = client.delete(f"/api/v1/merchant/agent-clients/{created['id']}", headers=auth(other))
    assert resp.status_code == 404


# ----------------------------------------------------------------------------- ACP


def test_acp_checkout_session_lifecycle(client: TestClient, shopify: FakeShopify) -> None:
    shop = new_shop()
    install(client, shop)
    key = issue(client, shop, "acp")["api_key"]

    resp = client.post(
        "/api/v1/acp/checkout_sessions",
        json={"items": [{"id": "101", "quantity": 1}]},
        headers=agent_headers(key, "create-1"),
    )
    assert resp.status_code == 201, resp.text
    assert resp.headers["Idempotency-Key"] == "create-1"
    session = resp.json()
    sid = session["id"]
    assert session["status"] == "not_ready_for_payment"
    assert {m["param"] for m in session["messages"]} == {"$.buyer.email", "$.fulfillment_address"}

    resp = client.post(
        f"/api/v1/acp/checkout_sessions/{sid}",
        json={
            "buyer": {"name": "Ada Lovelace", "email": "ada@example.com"},
            "fulfillment_address": ADDRESS_ACP,
        },
        headers=agent_headers(key, "update-1"),
    )
    session = resp.json()
    assert resp.status_code == 200 and session["status"] == "ready_for_payment"
    assert session["fulfillment_option_id"] == "shopify-Standard-5.00"

    resp = client.post(
        f"/api/v1/acp/checkout_sessions/{sid}",
        json={"fulfillment_option_id": "shopify-Express-15.00"},
        headers=agent_headers(key, "update-2"),
    )
    totals = {t["type"]: t["amount"] for t in resp.json()["totals"]}
    assert totals["fulfillment"] == 1500 and totals["total"] == 14850

    got = client.get(f"/api/v1/acp/checkout_sessions/{sid}", headers=agent_headers(key)).json()
    assert got["fulfillment_option_id"] == "shopify-Express-15.00"

    resp = client.post(
        f"/api/v1/acp/checkout_sessions/{sid}/complete",
        json={"payment_data": {"token": "spt_x", "provider": "stripe"}},
        headers=agent_headers(key, "complete-1"),
    )
    assert resp.status_code == 501 and resp.json()["code"] == "not_implemented"

    resp = client.post(
        f"/api/v1/acp/checkout_sessions/{sid}/cancel", headers=agent_headers(key, "cancel-1")
    )
    assert resp.status_code == 200 and resp.json()["status"] == "canceled"
    resp = client.post(
        f"/api/v1/acp/checkout_sessions/{sid}/cancel", headers=agent_headers(key, "cancel-2")
    )
    assert resp.status_code == 405
    assert resp.json()["type"] == "invalid_request"


def test_idempotency(client: TestClient, shopify: FakeShopify) -> None:
    shop = new_shop()
    install(client, shop)
    key = issue(client, shop, "acp")["api_key"]
    body = {"items": [{"id": "101", "quantity": 1}]}

    first = client.post(
        "/api/v1/acp/checkout_sessions", json=body, headers=agent_headers(key, "same")
    )
    calls = len(shopify.calls)
    replay = client.post(
        "/api/v1/acp/checkout_sessions", json=body, headers=agent_headers(key, "same")
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json()
    assert len(shopify.calls) == calls  # not re-run

    resp = client.post(
        "/api/v1/acp/checkout_sessions",
        json={"items": [{"id": "101", "quantity": 2}]},
        headers=agent_headers(key, "same"),
    )
    assert resp.status_code == 409 and resp.json()["code"] == "request_not_idempotent"

    resp = client.post("/api/v1/acp/checkout_sessions", json=body, headers=agent_headers(key))
    assert resp.status_code == 400 and "Idempotency-Key" in resp.json()["message"]

    # Validation failures are stored too, so a retry gets the same answer.
    bad = {"items": [{"id": "sku-1", "quantity": 1}]}
    for _ in range(2):
        resp = client.post(
            "/api/v1/acp/checkout_sessions", json=bad, headers=agent_headers(key, "bad")
        )
        assert resp.status_code == 400 and resp.json()["param"] == "$.items[0].id"


def test_agent_auth(client: TestClient, shopify: FakeShopify) -> None:
    shop = new_shop()
    install(client, shop)
    acp = issue(client, shop, "acp")
    ucp_key = issue(client, shop, "ucp")["api_key"]
    url = "/api/v1/acp/checkout_sessions/" + str(uuid.uuid4())

    resp = client.get(url)
    assert resp.status_code == 401 and resp.json()["type"] == "invalid_request"
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    forged = "acg_live_000000000000_" + "x" * 43
    assert client.get(url, headers=agent_headers(forged)).status_code == 401
    assert client.get(url, headers=agent_headers(ucp_key)).status_code == 403
    assert client.get(url, headers=agent_headers(acp["api_key"])).status_code == 404

    client.delete(f"/api/v1/merchant/agent-clients/{acp['id']}", headers=auth(shop))
    assert client.get(url, headers=agent_headers(acp["api_key"])).status_code == 401

    catalog_only = issue(client, shop, "acp", scopes=["catalog:read"])["api_key"]
    assert client.get(url, headers=agent_headers(catalog_only)).status_code == 403


def test_carts_are_isolated_per_merchant(client: TestClient, shopify: FakeShopify) -> None:
    a, b = new_shop(), new_shop()
    install(client, a)
    install(client, b)
    key_a, key_b = issue(client, a, "acp")["api_key"], issue(client, b, "acp")["api_key"]
    sid = client.post(
        "/api/v1/acp/checkout_sessions",
        json={"items": [{"id": "101", "quantity": 1}]},
        headers=agent_headers(key_a, "k"),
    ).json()["id"]
    assert (
        client.get(f"/api/v1/acp/checkout_sessions/{sid}", headers=agent_headers(key_b)).status_code
        == 404
    )


# ----------------------------------------------------------------------------- UCP


def test_ucp_checkout_and_discovery(client: TestClient, shopify: FakeShopify) -> None:
    shop = new_shop()
    install(client, shop)
    key = issue(client, shop, "ucp")["api_key"]
    headers = {**agent_headers(key, "u1"), "UCP-Agent": 'profile="https://agent.example/p"'}

    resp = client.post(
        "/api/v1/ucp/checkout-sessions",
        json={
            "line_items": [{"item": {"id": "101"}, "quantity": 1}],
            "buyer": {"email": "ada@example.com"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    checkout = resp.json()
    assert checkout["status"] == "incomplete"
    assert checkout["fulfillment"]["methods"][0]["destinations"] == []

    destination = {
        "street_address": "1 Main St",
        "address_locality": "Austin",
        "address_region": "TX",
        "postal_code": "78701",
        "address_country": "US",
    }
    resp = client.put(
        f"/api/v1/ucp/checkout-sessions/{checkout['id']}",
        json={
            "line_items": [{"id": "li_101", "item": {"id": "101"}, "quantity": 2}],
            "fulfillment": {"methods": [{"type": "shipping", "destinations": [destination]}]},
        },
        headers={**headers, "Idempotency-Key": "u2"},
    )
    checkout = resp.json()
    assert checkout["status"] == "ready_for_complete"
    assert checkout["line_items"][0]["quantity"] == 2
    assert {t["type"]: t["amount"] for t in checkout["totals"]}["total"] == 26950

    resp = client.put(
        f"/api/v1/ucp/checkout-sessions/{uuid.uuid4()}",
        json={},
        headers={**headers, "Idempotency-Key": "u3"},
    )
    assert resp.status_code == 404 and resp.json()["ucp"]["status"] == "error"

    profile = client.get("/.well-known/ucp").json()["ucp"]
    service = profile["services"]["dev.ucp.shopping"][0]
    assert service["rest"]["endpoint"].endswith("/api/v1/ucp")
    assert service["mcp"]["endpoint"].endswith("/mcp")


# ----------------------------------------------------------------------------- MCP


def mcp_call(client: TestClient, key: str | None, method: str, params: dict | None = None):
    headers = dict(MCP_HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers,
    )


def tool(client: TestClient, key: str, name: str, **arguments: Any) -> dict[str, Any]:
    resp = mcp_call(client, key, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    text = result["content"][0]["text"]
    if result["isError"]:
        return {"error": True, "text": text}
    return {"error": False, **(result.get("structuredContent") or json.loads(text))}


def test_mcp_tools(client: TestClient, shopify: FakeShopify) -> None:
    shop = new_shop()
    install(client, shop)
    key = issue(client, shop, "mcp")["api_key"]

    assert mcp_call(client, None, "tools/list").status_code == 401
    acp_key = issue(client, shop, "acp")["api_key"]
    assert mcp_call(client, acp_key, "tools/list").status_code == 403

    tools = {t["name"] for t in mcp_call(client, key, "tools/list").json()["result"]["tools"]}
    assert tools == {
        "search_products",
        "get_product",
        "create_cart",
        "update_cart",
        "get_cart",
        "cancel_cart",
    }

    found = tool(client, key, "search_products", query="jacket")
    assert found["products"][0]["variants"][0]["variant_id"].endswith("/101")

    cart = tool(
        client,
        key,
        "create_cart",
        lines=[{"variant_id": "101", "quantity": 1}],
        email="ada@example.com",
        shipping_address={
            "address1": "1 Main St",
            "city": "Austin",
            "province_code": "TX",
            "country_code": "US",
            "zip": "78701",
        },
    )
    assert cart["status"] == "ready", cart
    assert (cart["subtotal"], cart["shipping"], cart["tax"], cart["total"]) == (
        "120.00",
        "5.00",
        "12.50",
        "137.50",
    )

    cart = tool(
        client,
        key,
        "update_cart",
        cart_id=cart["cart_id"],
        shipping_option_id="shopify-Express-15.00",
    )
    assert cart["total"] == "148.50"
    assert tool(client, key, "get_cart", cart_id=cart["cart_id"])["selected_shipping_option"] == (
        "shopify-Express-15.00"
    )
    assert tool(client, key, "cancel_cart", cart_id=cart["cart_id"])["status"] == "canceled"

    failed = tool(client, key, "update_cart", cart_id=cart["cart_id"], email="b@example.com")
    assert failed["error"] and "canceled" in failed["text"]
    assert tool(client, key, "get_cart", cart_id="nope")["error"]

    limited = issue(client, shop, "mcp", scopes=["catalog:read"])["api_key"]
    denied = tool(client, limited, "create_cart", lines=[{"variant_id": "101", "quantity": 1}])
    assert denied["error"] and "checkout" in denied["text"]
