import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import FRONTEND_URL, PRIVATE_PEM
from tests.keys import mint

PRODUCTS = json.loads((Path(__file__).parent.parent / "fixtures/shopify/products.json").read_text())
GRAPHQL_PATH = "/admin/api/2026-10/graphql.json"


@pytest.fixture
def client(app_env: None) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def auth(shop: str, scope: list[str] | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint(PRIVATE_PEM, shop, scope=scope)}"}


def install(client: TestClient, shop: str) -> None:
    resp = client.post(
        "/internal/merchants/sync",
        json={"shop": shop, "access_token": "shpat_install_time", "scopes": "read_products"},
        headers=auth(shop, ["internal:sync"]),
    )
    assert resp.status_code == 200, resp.text


def mock_shopify(router: respx.Router, shop: str, products: list[dict[str, Any]]) -> respx.Route:
    """Serve products in pages of 2 plus shop/collections queries."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Shopify-Access-Token"] == "shpat_fresh"
        body = json.loads(request.content)
        query, variables = body["query"], body["variables"]
        if "ShopForLlmTxt" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "shop": {
                            "name": "Acme Outfitters",
                            "description": "Outdoor gear.",
                            "url": f"https://{shop}",
                            "primaryDomain": {
                                "url": "https://acme.example",
                                "host": "acme.example",
                            },
                            "currencyCode": "USD",
                            "contactEmail": None,
                            "shopPolicies": [],
                        }
                    }
                },
            )
        if "CollectionsForLlmTxt" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "collections": {
                            "nodes": [
                                {
                                    "title": "Jackets",
                                    "handle": "jackets",
                                    "description": "",
                                    "productsCount": {"count": 1},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )
        start = int(variables.get("cursor") or 0)
        page = products[start : start + 2]
        has_next = start + 2 < len(products)
        return httpx.Response(
            200,
            json={
                "data": {
                    "products": {
                        "nodes": page,
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": str(start + 2) if has_next else None,
                        },
                    }
                }
            },
        )

    router.post(f"{FRONTEND_URL}/internal/admin-token").respond(
        200, json={"access_token": "shpat_fresh", "expires_at": None}
    )
    return router.post(f"https://{shop}{GRAPHQL_PATH}").mock(side_effect=handler)


def catalog() -> list[dict[str, Any]]:
    return [copy.deepcopy(PRODUCTS[k]) for k in ("complete", "sparse", "partial")]


def test_audit_run_end_to_end(client: TestClient) -> None:
    shop = "audit-e2e.myshopify.com"
    install(client, shop)

    empty = client.get("/api/v1/merchant/audits/latest", headers=auth(shop)).json()
    assert empty["run"] is None and empty["ai_discovery_score"] is None

    with respx.mock() as router:
        graphql = mock_shopify(router, shop, catalog())
        started = client.post("/api/v1/merchant/audits", headers=auth(shop))
    assert started.status_code == 202
    assert graphql.call_count == 2  # two pages of products

    summary = client.get("/api/v1/merchant/audits/latest", headers=auth(shop)).json()
    assert summary["run"]["status"] == "completed"
    assert summary["run"]["products_scanned"] == 3
    assert summary["ai_discovery_score"] == "52.33"
    assert summary["grade_counts"] == {"excellent": 1, "poor": 2}
    assert summary["dimension_averages"]["description"]["max"] == 20.0
    top = {i["code"]: i for i in summary["top_issues"]}
    assert top["reviews_missing"]["count"] == 2
    assert top["reviews_missing"]["message"].startswith("No product rating")

    page = client.get(
        "/api/v1/merchant/audits/latest/products?max_score=80&limit=10", headers=auth(shop)
    ).json()
    assert page["total"] == 2
    assert [p["score"] for p in page["items"]] == [10, 47]  # worst first
    assert page["items"][0]["admin_url"] == (
        "https://admin.shopify.com/store/audit-e2e/products/1002"
    )
    filtered = client.get(
        "/api/v1/merchant/audits/latest/products?issue=description_short", headers=auth(shop)
    ).json()
    assert [p["title"] for p in filtered["items"]] == ["Trail Running Socks"]


def test_failed_audit_is_marked_failed(client: TestClient) -> None:
    shop = "audit-fail.myshopify.com"
    install(client, shop)
    with respx.mock() as router:
        router.post(f"{FRONTEND_URL}/internal/admin-token").respond(
            200, json={"access_token": "shpat_fresh"}
        )
        router.post(f"https://{shop}{GRAPHQL_PATH}").respond(403)
        client.post("/api/v1/merchant/audits", headers=auth(shop))
    summary = client.get("/api/v1/merchant/audits/latest", headers=auth(shop)).json()
    assert summary["run"]["status"] == "failed"
    assert summary["completed_run"] is None


def test_mapping_rules_crud_and_effect_on_score(client: TestClient) -> None:
    shop = "audit-rules.myshopify.com"
    install(client, shop)
    bad = client.post(
        "/api/v1/merchant/mapping-rules",
        json={"source_type": "tag", "match_pattern": "(", "target_property": "material"},
        headers=auth(shop),
    )
    assert bad.status_code == 422
    created = client.post(
        "/api/v1/merchant/mapping-rules",
        json={"source_type": "tag", "match_pattern": "brand:(.+)", "target_property": "brand"},
        headers=auth(shop),
    )
    assert created.status_code == 201
    assert len(client.get("/api/v1/merchant/mapping-rules", headers=auth(shop)).json()) == 1

    sparse = copy.deepcopy(PRODUCTS["sparse"])
    sparse["tags"] = ["brand:acme"]
    with respx.mock() as router:
        mock_shopify(router, shop, [sparse])
        client.post("/api/v1/merchant/audits", headers=auth(shop))
    item = client.get("/api/v1/merchant/audits/latest/products", headers=auth(shop)).json()
    assert "brand_missing" not in item["items"][0]["missing_fields"]

    rule_id = created.json()["id"]
    other = "someone-else.myshopify.com"
    install(client, other)
    assert (
        client.delete(f"/api/v1/merchant/mapping-rules/{rule_id}", headers=auth(other)).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/merchant/mapping-rules/{rule_id}", headers=auth(shop)).status_code
        == 204
    )


def test_llm_txt_streams_caches_and_includes_faqs(client: TestClient) -> None:
    shop = "llm-e2e.myshopify.com"
    install(client, shop)
    faqs = {"faqs": [{"question": "Do you ship to Canada?", "answer": "Yes."}]}
    assert client.put("/api/v1/merchant/faqs", json=faqs, headers=auth(shop)).status_code == 200
    assert client.get("/api/v1/merchant/faqs", headers=auth(shop)).json() == faqs

    with respx.mock() as router:
        graphql = mock_shopify(router, shop, catalog())
        resp = client.get(f"/api/v1/agent-gateway/llm.txt?shop={shop}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "max-age=600" in resp.headers["cache-control"]
        body = resp.text
        calls_after_first = graphql.call_count
        again = client.get(f"/api/v1/agent-gateway/llm.txt?shop={shop.upper()}")
        assert again.text == body
        assert graphql.call_count == calls_after_first  # served from cache

    assert body.startswith("# Acme Outfitters")
    assert "Q: Do you ship to Canada?" in body
    assert "- [Jackets](https://acme.example/collections/jackets) (1 products)" in body
    assert body.count("### ") == 3


def test_llm_txt_rejects_unknown_and_malformed_shops(client: TestClient) -> None:
    assert client.get("/api/v1/agent-gateway/llm.txt?shop=evil.com").status_code == 400
    assert client.get("/api/v1/agent-gateway/llm.txt?shop=nobody.myshopify.com").status_code == 404


def test_llm_txt_upstream_auth_failure_is_503(client: TestClient) -> None:
    shop = "llm-down.myshopify.com"
    install(client, shop)
    with respx.mock() as router:
        router.post(f"{FRONTEND_URL}/internal/admin-token").respond(404)
        resp = client.get(f"/api/v1/agent-gateway/llm.txt?shop={shop}")
    assert resp.status_code == 503
