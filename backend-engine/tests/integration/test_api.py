import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import PRIVATE_PEM, WEBHOOK_SECRET
from tests.keys import mint

SHOP = "acme-test.myshopify.com"
STORE = f"https://{SHOP}"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "storefront"


@pytest.fixture
def client(app_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def _allow(_host: str) -> None:
        return None

    monkeypatch.setattr("app.services.installation_checker.assert_public_host", _allow)
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def auth(scope: list[str] | None = None, shop: str = SHOP) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint(PRIVATE_PEM, shop, scope=scope)}"}


def sync(client: TestClient, shop: str = SHOP) -> None:
    resp = client.post(
        "/internal/merchants/sync",
        json={"shop": shop, "access_token": "shpat_test_token_123", "scopes": "read_products"},
        headers=auth(["internal:sync"], shop),
    )
    assert resp.status_code == 200, resp.text


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "database": "ok"}


def test_merchant_routes_require_jwt(client: TestClient) -> None:
    assert client.get("/api/v1/merchant/me").status_code == 401
    bad = {"Authorization": "Bearer not.a.jwt"}
    assert client.get("/api/v1/merchant/me", headers=bad).status_code == 401


def test_unknown_merchant_is_forbidden(client: TestClient) -> None:
    resp = client.get("/api/v1/merchant/me", headers=auth(shop="nobody.myshopify.com"))
    assert resp.status_code == 403


def test_sync_rejects_mismatched_subject(client: TestClient) -> None:
    resp = client.post(
        "/internal/merchants/sync",
        json={"shop": "victim.myshopify.com", "access_token": "shpat_xxxxxxxxxxxx"},
        headers=auth(["internal:sync"], "attacker.myshopify.com"),
    )
    assert resp.status_code == 403


def test_sync_requires_internal_scope(client: TestClient) -> None:
    resp = client.post(
        "/internal/merchants/sync",
        json={"shop": SHOP, "access_token": "shpat_xxxxxxxxxxxx"},
        headers=auth(),
    )
    assert resp.status_code == 403


def test_replayed_token_rejected(client: TestClient) -> None:
    sync(client)
    headers = auth()
    assert client.get("/api/v1/merchant/me", headers=headers).status_code == 200
    assert client.get("/api/v1/merchant/me", headers=headers).status_code == 401


def test_installation_check_persists_and_returns_latest(client: TestClient) -> None:
    sync(client)
    with respx.mock() as router:
        router.get(f"{STORE}/").respond(200, html=(FIXTURES / "home.html").read_text())
        router.get(f"{STORE}/products.json", params={"limit": "1"}).respond(
            200, json={"products": [{"handle": "summit-shell"}]}
        )
        router.get(f"{STORE}/products/summit-shell").respond(
            200, html=(FIXTURES / "product_good.html").read_text()
        )
        router.get(f"{STORE}/robots.txt").respond(200, text="User-agent: *\nDisallow: /admin\n")
        router.get(f"{STORE}/llms.txt").respond(200, text="# Acme\n")

        resp = client.post("/api/v1/merchant/installation-checks", json={}, headers=auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True
    assert body["app_block_enabled"] is True
    assert "Product" in body["schemas_found"]
    assert body["details"]["findings"] == []

    latest = client.get("/api/v1/merchant/installation-checks/latest", headers=auth())
    assert latest.status_code == 200
    assert latest.json()["id"] == body["id"]


def test_installation_check_rejects_private_targets(app_env: None) -> None:
    from app.main import create_app

    with TestClient(create_app()) as c:
        sync(c)
        resp = c.post(
            "/api/v1/merchant/installation-checks",
            json={"store_url": "http://127.0.0.1"},
            headers=auth(),
        )
    assert resp.status_code == 422


def _signed(body: dict) -> tuple[bytes, str]:  # type: ignore[type-arg]
    raw = json.dumps(body).encode()
    sig = base64.b64encode(hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest())
    return raw, sig.decode()


def test_uninstall_webhook_is_verified_deduped_and_revokes_access(client: TestClient) -> None:
    sync(client)
    raw, sig = _signed({"id": 1})
    webhook_id = str(uuid.uuid4())
    headers = {
        "X-Shopify-Hmac-Sha256": sig,
        "X-Shopify-Topic": "app/uninstalled",
        "X-Shopify-Shop-Domain": SHOP,
        "X-Shopify-Webhook-Id": webhook_id,
        "Content-Type": "application/json",
    }
    bad = {**headers, "X-Shopify-Hmac-Sha256": "AAAA"}
    assert client.post("/api/v1/webhooks/shopify", content=raw, headers=bad).status_code == 401

    first = client.post("/api/v1/webhooks/shopify", content=raw, headers=headers)
    assert first.json() == {"status": "ok"}
    second = client.post("/api/v1/webhooks/shopify", content=raw, headers=headers)
    assert second.json() == {"status": "duplicate"}

    assert client.get("/api/v1/merchant/me", headers=auth()).status_code == 403
