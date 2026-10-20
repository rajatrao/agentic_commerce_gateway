"""End to end: import and audit a catalog, enrich it, review suggestions, write metafields."""

import copy
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.services.ai_enricher import AIAttribute, AIEnrichment, AIIssue
from tests.conftest import FRONTEND_URL
from tests.integration.test_catalog import GRAPHQL_PATH, auth, install, wait_for_audit
from tests.integration.test_order_linking import webhook

P = "gid://shopify/Product/"

BACKPACK: dict[str, Any] = {
    "id": f"{P}501",
    "title": "Adventure Backpack",
    "handle": "adventure-backpack",
    "status": "ACTIVE",
    "vendor": "Acme",
    "productType": "Backpacks",
    "tags": ["hiking", "travel", "packs"],
    "description": (
        "A versatile backpack for hiking and everyday adventures with a spacious interior and "
        "dedicated laptop compartment. Weighs 1.2 kg."
    ),
    "descriptionHtml": "",
    "onlineStoreUrl": "https://acme.example/products/adventure-backpack",
    "updatedAt": "2026-10-01T00:00:00Z",
    "category": None,
    "seo": {"title": None, "description": None},
    "options": [{"name": "Title", "values": ["Default Title"]}],
    "priceRangeV2": {},
    "totalInventory": 5,
    "media": {"nodes": []},
    "variants": {
        "nodes": [
            {
                "id": "gid://shopify/ProductVariant/5011",
                "title": "Default Title",
                "sku": "ADV-1",
                "barcode": None,
                "price": "89.00",
                "compareAtPrice": None,
                "availableForSale": True,
                "inventoryQuantity": 5,
                "inventoryItem": {"tracked": True},
            }
        ]
    },
    "metafields": {"nodes": []},
}

TENT: dict[str, Any] = copy.deepcopy(BACKPACK) | {
    "id": f"{P}502",
    "title": "Ridge 2 Tent",
    "handle": "ridge-2",
    "productType": "Tents",
    "description": "A 2-person 3-season tent. Packs down small.",
    "metafields": {
        "nodes": [
            {
                "namespace": "custom",
                "key": "sleeping_capacity",
                "type": "number_integer",
                "value": "3",
                "compareDigest": "d-tent",
            }
        ]
    },
}


class FakeStore:
    """Enough of the Admin GraphQL API for audits, enrichment and metafield writes."""

    def __init__(self, shop: str, products: list[dict[str, Any]]) -> None:
        self.shop = shop
        self.products = {p["id"]: copy.deepcopy(p) for p in products}
        self.definitions: dict[tuple[str, str], str] = {}
        self.writes: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    def handle(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "AuditProducts" in query:
            return {
                "products": {
                    "nodes": list(self.products.values()),
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        if "EnrichProduct" in query or "ProductMetafieldsForWrite" in query:
            return {"product": copy.deepcopy(self.products.get(variables["id"]))}
        if "ProductMetafieldDefinitions" in query:
            nodes = [
                {"id": f"d{i}", "name": k, "namespace": ns, "key": k, "type": {"name": t}}
                for i, ((ns, k), t) in enumerate(self.definitions.items())
            ]
            return {
                "metafieldDefinitions": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        if "CreateProductMetafieldDefinition" in query:
            d = variables["definition"]
            self.definitions[(d["namespace"], d["key"])] = d["type"]
            return {"metafieldDefinitionCreate": {"createdDefinition": {}, "userErrors": []}}
        if "SetProductMetafields" in query:
            errors = []
            for i, m in enumerate(variables["metafields"]):
                nodes = self.products[m["ownerId"]]["metafields"]["nodes"]
                existing = next(
                    (n for n in nodes if (n["namespace"], n["key"]) == (m["namespace"], m["key"])),
                    None,
                )
                if existing and m.get("compareDigest") not in (None, existing["compareDigest"]):
                    errors.append(
                        {"message": "stale value", "code": "STALE_OBJECT", "elementIndex": i}
                    )
                    continue
                if existing:
                    nodes.remove(existing)
                nodes.append(
                    {
                        "namespace": m["namespace"],
                        "key": m["key"],
                        "type": m["type"],
                        "value": m["value"],
                        "compareDigest": f"d{len(self.writes)}",
                    }
                )
                self.writes.append(m)
            return {"metafieldsSet": {"metafields": variables["metafields"], "userErrors": errors}}
        if "DeleteProductMetafields" in query:
            for m in variables["metafields"]:
                nodes = self.products[m["ownerId"]]["metafields"]["nodes"]
                nodes[:] = [
                    n for n in nodes if (n["namespace"], n["key"]) != (m["namespace"], m["key"])
                ]
                self.deletes.append(m)
            return {"metafieldsDelete": {"deletedMetafields": [], "userErrors": []}}
        raise AssertionError(f"unexpected query {query[:60]}")


@pytest.fixture
def client(app_env: None) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def store() -> Iterator[FakeStore]:
    fake = FakeStore("", [BACKPACK, TENT])

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


def audited_shop(client: TestClient, shop: str) -> None:
    install(client, shop)
    assert client.post("/api/v1/merchant/audits", headers=auth(shop)).status_code == 202
    assert wait_for_audit(client, shop)["run"]["status"] == "completed"


def wait_for_job(client: TestClient, shop: str, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while True:
        job: dict[str, Any] = client.get(
            f"/api/v1/merchant/catalog/enrichment-jobs/{job_id}", headers=auth(shop)
        ).json()
        if job["status"] not in ("queued", "running"):
            return job
        assert time.monotonic() < deadline, job
        time.sleep(0.05)


def suggestions(client: TestClient, shop: str, **params: str) -> dict[str, dict[str, Any]]:
    page = client.get(
        "/api/v1/merchant/catalog/suggestions", params=params, headers=auth(shop)
    ).json()
    return {f"{s['product_gid'].rsplit('/', 1)[1]}:{s['attribute_key']}": s for s in page["items"]}


C = "/api/v1/merchant/catalog"


def test_audit_persists_findings_scores_and_snapshots(client: TestClient, store: FakeStore) -> None:
    shop = "ci-audit.myshopify.com"
    audited_shop(client, shop)

    overview = client.get(f"{C}/overview", headers=auth(shop)).json()
    assert overview["has_audit"] and overview["products_analyzed"] == 2
    assert overview["readiness_score"] is not None
    assert overview["findings_by_severity"]["critical"] >= 1  # backpack capacity is missing
    assert overview["products_missing_required"] == 2
    tests = {t["query"]: t for t in overview["discovery_tests"]}
    assert tests["lightweight hiking backpack around 30 liters"]["supported"] == 0
    assert len(overview["trend"]) == 1

    products = client.get(f"{C}/products", headers=auth(shop)).json()
    assert products["total"] == 2
    by_title = {p["title"]: p for p in products["items"]}
    assert by_title["Adventure Backpack"]["schema_key"] == "backpacks"
    assert by_title["Ridge 2 Tent"]["schema_key"] == "tents"

    contradictions = client.get(
        f"{C}/products", params={"code": "attribute_contradiction"}, headers=auth(shop)
    ).json()
    assert [p["title"] for p in contradictions["items"]] == ["Ridge 2 Tent"]  # 3 vs "2-person"

    detail = client.get(f"{C}/products/501", headers=auth(shop)).json()
    attrs = {a["key"]: a for a in detail["attributes"]}
    assert attrs["capacity"]["current_display"] == "Not set"
    assert attrs["capacity"]["metafield"] == "custom.capacity_liters"
    finding = next(f for f in detail["findings"] if f["attribute_key"] == "weight")
    assert finding["code"] == "attribute_buried" and finding["evidence"] == "Weighs 1.2 kg."
    assert detail["readiness_breakdown"]["completeness"]["weight"] == 30

    critical = client.get(f"{C}/findings", params={"severity": "critical"}, headers=auth(shop))
    assert all(f["severity"] == "critical" for f in critical.json()["items"])
    urgent = client.get(
        f"{C}/products", params={"severity": "critical,high"}, headers=auth(shop)
    ).json()
    assert urgent["total"] >= 1
    bad = client.get(f"{C}/products", params={"severity": "urgent"}, headers=auth(shop))
    assert bad.status_code == 422

    runs = client.get(f"{C}/runs", headers=auth(shop)).json()
    assert [r["status"] for r in runs] == ["completed"] and runs[0]["avg_readiness"] is not None


def test_enrich_review_publish_and_revert(client: TestClient, store: FakeStore) -> None:
    shop = "ci-flow.myshopify.com"
    audited_shop(client, shop)

    started = client.post(
        f"{C}/enrichment-jobs", json={"product_gids": ["501", f"{P}502"]}, headers=auth(shop)
    )
    assert started.status_code == 202, started.text
    assert started.json()["use_ai"] is False  # no AI key configured in tests
    job = wait_for_job(client, shop, started.json()["id"])
    assert job["status"] == "completed" and job["succeeded"] == 2

    s = suggestions(client, shop)
    assert s["501:capacity"]["proposed_value"] is None  # unknown, not invented
    assert s["501:capacity"]["validation_status"] == "unknown"
    assert s["501:weight"]["proposed_value"] == {"value": 1.2, "unit": "kg"}
    assert s["501:weight"]["evidence"] == "Weighs 1.2 kg."
    assert s["501:laptop_compartment"]["proposed_value"] is True
    assert s["502:sleeping_capacity"]["proposed_value"] == 2
    assert s["502:sleeping_capacity"]["validation_status"] == "warning"  # replaces 3

    review = client.post(
        f"{C}/suggestions/review",
        json={
            "actions": [
                {"id": s["501:weight"]["id"], "action": "approve"},
                {"id": s["501:capacity"]["id"], "action": "approve"},  # unknown: refused
                {"id": s["501:capacity"]["id"], "action": "edit", "value": "28 L"},
                {"id": s["501:laptop_compartment"]["id"], "action": "reject"},
                {"id": s["502:sleeping_capacity"]["id"], "action": "approve"},
            ]
        },
        headers=auth(shop),
    ).json()
    assert [r["ok"] for r in review["results"]] == [True, False, True, True, True]
    bad_edit = client.post(
        f"{C}/suggestions/review",
        json={"actions": [{"id": s["501:material"]["id"], "action": "edit", "value": ""}]},
        headers=auth(shop),
    ).json()
    assert bad_edit["results"][0]["ok"] is False

    preview = client.post(f"{C}/publish/preview", json={}, headers=auth(shop)).json()
    items = {i["attribute_key"]: i for i in preview["items"]}
    assert items["capacity"]["action"] == "create" and items["capacity"]["value"] == "28"
    assert items["capacity"]["creates_definition"]
    assert items["weight"]["type"] == "weight"
    assert items["sleeping_capacity"]["action"] == "update"
    assert items["sleeping_capacity"]["current_value"] == "3"
    assert store.writes == []  # preview writes nothing

    refused = client.post(f"{C}/publish", json={}, headers=auth(shop))
    assert refused.status_code == 422  # confirmation required
    result = client.post(f"{C}/publish", json={"confirm": True}, headers=auth(shop)).json()
    assert result == {"published": 3, "unchanged": 0, "failed": 0}
    written = {(w["key"], w["value"]) for w in store.writes}
    assert ("capacity_liters", "28") in written
    assert ("weight", json.dumps({"value": 1.2, "unit": "kg"})) in written
    tent_write = next(w for w in store.writes if w["key"] == "sleeping_capacity")
    assert tent_write["compareDigest"] == "d-tent"
    assert store.definitions[("custom", "capacity_liters")] == "number_decimal"

    again = client.post(f"{C}/publish", json={"confirm": True}, headers=auth(shop)).json()
    assert again == {"published": 0, "unchanged": 0, "failed": 0}  # nothing approved is left

    published = suggestions(client, shop, status="published")
    assert set(published) == {"501:weight", "501:capacity", "502:sleeping_capacity"}
    detail = client.get(f"{C}/products/501", headers=auth(shop)).json()
    attrs = {a["key"]: a for a in detail["attributes"]}
    assert attrs["capacity"]["current_display"] == "28 l"

    writes = client.get(f"{C}/changes", params={"writes_only": True}, headers=auth(shop)).json()
    tent_change = next(c for c in writes["items"] if c["attribute_key"] == "sleeping_capacity")
    assert tent_change["before"] == {"value": "3", "type": "number_integer"}
    assert tent_change["revertible"]
    reverted = client.post(f"{C}/changes/{tent_change['id']}/revert", headers=auth(shop))
    assert reverted.status_code == 200, reverted.text
    tent_nodes = store.products[f"{P}502"]["metafields"]["nodes"]
    assert [n["value"] for n in tent_nodes if n["key"] == "sleeping_capacity"] == ["3"]
    twice = client.post(f"{C}/changes/{tent_change['id']}/revert", headers=auth(shop))
    assert twice.status_code == 409

    capacity_change = next(c for c in writes["items"] if c["attribute_key"] == "capacity")
    client.post(f"{C}/changes/{capacity_change['id']}/revert", headers=auth(shop))
    assert store.deletes[-1]["key"] == "capacity_liters"  # created by us: deleted on revert


class FakeEnricher:
    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    async def enrich(self, sources: Any, metafields: Any, schema: Any) -> AIEnrichment:
        self.calls += 1
        return AIEnrichment(
            attributes=[
                AIAttribute(
                    key="material",
                    status="stated",
                    value="Cordura",
                    evidence_quote="Made from Cordura nylon.",  # not in the listing
                    evidence_source="description",
                    confidence=0.9,
                    explanation="invented",
                ),
                AIAttribute(
                    key="intended_use",
                    status="stated",
                    value="hiking, everyday",
                    evidence_quote="A versatile backpack for hiking and everyday adventures",
                    evidence_source="description",
                    confidence=0.92,
                    explanation="stated use",
                ),
            ],
            issues=[
                AIIssue(
                    kind="ambiguity",
                    attribute_key="capacity",
                    severity="medium",
                    message="'Spacious interior' does not give a capacity.",
                    evidence_quote="spacious interior",
                    confidence=0.85,
                )
            ],
        )


def test_ai_enrichment_is_validated_and_isolated_per_shop(
    client: TestClient, store: FakeStore
) -> None:
    shop, other = "ci-ai.myshopify.com", "ci-other.myshopify.com"
    audited_shop(client, shop)
    install(client, other)
    fake = FakeEnricher()
    client.app.state.enricher = fake  # type: ignore[attr-defined]
    client.app.state.enrichment_worker.enricher = fake  # type: ignore[attr-defined]

    job = client.post(f"{C}/enrichment-jobs", json={"lowest_readiness": 5}, headers=auth(shop))
    assert job.json()["use_ai"] is True
    wait_for_job(client, shop, job.json()["id"])
    assert fake.calls == 2

    s = suggestions(client, shop)
    assert s["501:intended_use"]["method"] == "llm:fake-model"
    assert s["501:intended_use"]["proposed_value"] == ["hiking", "everyday"]
    assert s["501:material"]["proposed_value"] is None
    assert any("rejected" in e for e in s["501:material"]["validation_errors"])
    ai_findings = client.get(f"{C}/findings", params={"method": "ai"}, headers=auth(shop))
    assert {f["code"] for f in ai_findings.json()["items"]} == {"ai_ambiguity"}

    # Another shop sees none of it and cannot act on it.
    assert suggestions(client, other) == {}
    foreign = client.post(
        f"{C}/suggestions/review",
        json={"actions": [{"id": s["501:intended_use"]["id"], "action": "approve"}]},
        headers=auth(other),
    ).json()
    assert foreign["results"][0] == {
        "id": s["501:intended_use"]["id"],
        "ok": False,
        "error": "suggestion not found",
    }
    assert client.get(f"{C}/products/501", headers=auth(other)).status_code == 404
    other_job = client.post(
        f"{C}/enrichment-jobs", json={"product_gids": ["501"]}, headers=auth(other)
    )
    assert other_job.status_code == 422  # not in that shop's catalog
    read_only = client.post(
        f"{C}/publish", json={"confirm": True}, headers=auth(shop, ["merchant:read"])
    )
    assert read_only.status_code == 403


def test_deleted_products_close_open_suggestions(client: TestClient, store: FakeStore) -> None:
    shop = "ci-webhook.myshopify.com"
    audited_shop(client, shop)
    job = client.post(f"{C}/enrichment-jobs", json={"product_gids": ["501"]}, headers=auth(shop))
    wait_for_job(client, shop, job.json()["id"])
    assert suggestions(client, shop)

    webhook(client, shop, "products/delete", {"id": 501})
    assert suggestions(client, shop) == {}
    assert suggestions(client, shop, status="superseded")
    products = client.get(f"{C}/products", headers=auth(shop)).json()
    assert [p["title"] for p in products["items"]] == ["Ridge 2 Tent"]

    webhook(
        client, shop, "products/update", {"id": 502, "title": "Ridge 2 Tent v2", "status": "active"}
    )
    titles = [p["title"] for p in client.get(f"{C}/products", headers=auth(shop)).json()["items"]]
    assert titles == ["Ridge 2 Tent v2"]


def test_schema_mapping_and_settings_configuration(client: TestClient, store: FakeStore) -> None:
    shop = "ci-config.myshopify.com"
    install(client, shop)
    schemas = {s["key"]: s for s in client.get(f"{C}/schemas", headers=auth(shop)).json()}
    assert {"backpacks", "tents", "general"} <= set(schemas)

    backpacks = schemas["backpacks"]["definition"]
    backpacks["attributes"][0]["importance"] = "high"
    saved = client.put(f"{C}/schemas/backpacks", json=backpacks, headers=auth(shop))
    assert saved.status_code == 200 and saved.json()["customized"]
    bad = dict(backpacks, attributes=[])
    assert client.put(f"{C}/schemas/backpacks", json=bad, headers=auth(shop)).status_code == 422
    assert client.delete(f"{C}/schemas/backpacks", headers=auth(shop)).status_code == 204

    mappings = client.get(f"{C}/mappings", params={"check_shopify": True}, headers=auth(shop))
    capacity = next(m for m in mappings.json() if m["attribute_key"] == "capacity")
    assert capacity["status"] == "will_create" and capacity["type"] == "number_decimal"
    incompatible = client.put(
        f"{C}/mappings/capacity",
        json={"namespace": "specs", "key": "capacity", "type": "boolean"},
        headers=auth(shop),
    )
    assert incompatible.status_code == 422
    clash = client.put(
        f"{C}/mappings/capacity",
        json={"namespace": "custom", "key": "weight", "type": "number_decimal"},
        headers=auth(shop),
    )
    assert clash.status_code == 409
    ok = client.put(
        f"{C}/mappings/capacity",
        json={"namespace": "specs", "key": "capacity", "type": "volume"},
        headers=auth(shop),
    )
    assert ok.status_code == 200 and ok.json()["source"] == "custom"

    settings = client.get(f"{C}/settings", headers=auth(shop)).json()
    assert settings["auto_publish"] is False and settings["ai_enabled"] is False
    updated = client.put(
        f"{C}/settings",
        json={"readiness_weights": {"foundation": 50}, "ready_threshold": 70},
        headers=auth(shop),
    ).json()
    assert updated["readiness_weights"]["foundation"] == 50 and updated["ready_threshold"] == 70
    bad_weights = client.put(
        f"{C}/settings",
        json={"readiness_weights": {"popularity": 10}, "ready_threshold": 70},
        headers=auth(shop),
    )
    assert bad_weights.status_code == 422
