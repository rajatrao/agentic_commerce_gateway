"""Auto-approval rules, taxonomy value matching and discovery result parsing."""

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.auto_approval import qualifies, unmet_conditions
from app.services.catalog_schema import AutoApproval, CategorySchema, builtin_schemas
from app.services.discovery_probe import ProbeUnavailable, QueryResult, SearchHit, _mcp_products
from app.services.shopify_client import ShopifyAuthError, ShopifyError
from app.services.taxonomy_writer import (
    TaxonomyRequest,
    TaxonomyWriter,
    find_attribute,
    match_values,
)


def candidate(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "attribute_key": "weight",
        "current_value": None,
        "proposed_value": {"value": 1.2, "unit": "kg"},
        "validation_status": "valid",
        "rule_confirmed": True,
        "confidence": Decimal("0.92"),
    }
    return SimpleNamespace(**(base | overrides))


def test_auto_approval_only_takes_confirmed_values_that_fill_gaps() -> None:
    rule = AutoApproval(enabled=True, min_confidence=0.9)
    assert qualifies(rule, candidate())
    assert not qualifies(rule.model_copy(update={"enabled": False}), candidate())
    assert not qualifies(None, candidate())
    cases = {
        "would replace an existing value": candidate(current_value={"value": 1, "unit": "kg"}),
        "not confirmed by rule-based extraction": candidate(rule_confirmed=False),
        "validation warnings": candidate(validation_status="warning"),
        "no value found": candidate(proposed_value=None),
        "confidence below 0.90": candidate(confidence=Decimal("0.75")),
    }
    for reason, c in cases.items():
        assert reason in unmet_conditions(rule, c)
    limited = AutoApproval(enabled=True, attributes=["capacity"])
    assert unmet_conditions(limited, candidate()) == ["attribute not included"]


def test_schemas_default_to_auto_approval_off_and_validate_attributes() -> None:
    assert all(not s.auto_approve.enabled for s in builtin_schemas().values())
    backpacks = builtin_schemas()["backpacks"].model_dump()
    backpacks["auto_approve"] = {"enabled": True, "attributes": ["nope"]}
    with pytest.raises(ValueError, match="unknown attributes"):
        CategorySchema.model_validate(backpacks)


CHOICES = [
    {"id": "v1", "name": "Nylon"},
    {"id": "v2", "name": "Polyester"},
    {"id": "v3", "name": "Recycled polyester"},
]


def test_taxonomy_values_match_whole_words_and_never_guess() -> None:
    assert match_values("Ripstop nylon", CHOICES) == ([CHOICES[0]], [])
    matched, unmatched = match_values(["recycled polyester", "canvas"], CHOICES)
    assert [m["id"] for m in matched] == ["v3"]  # exact name wins over the word "polyester"
    assert unmatched == ["canvas"]
    assert match_values("nylonish", CHOICES) == ([], ["nylonish"])


def test_taxonomy_attribute_lookup_by_name_or_unique_suffix() -> None:
    choice = "TaxonomyChoiceListAttribute"
    category = {
        "attributes": {
            "nodes": [
                {"__typename": choice, "id": "a1", "name": "Color"},
                {"__typename": choice, "id": "a2", "name": "Bag/Case material"},
                {"__typename": "TaxonomyMeasurementAttribute", "id": "a3", "name": "Material"},
            ]
        }
    }
    assert find_attribute(category, "color")["id"] == "a1"  # type: ignore[index]
    assert find_attribute(category, "Material")["id"] == "a2"  # type: ignore[index]
    assert find_attribute(category, "Activity") is None
    assert find_attribute(None, "Color") is None


def mcp_body(text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}


def test_storefront_search_results_are_parsed_defensively() -> None:
    body = mcp_body(
        json.dumps(
            {
                "products": [
                    {"product_id": "gid://shopify/Product/1", "title": "Pack"},
                    {"product_id": "not-a-gid", "title": "Ignored"},
                    {"product_id": "gid://shopify/Product/1", "title": "Duplicate"},
                ]
            }
        )
    )
    assert _mcp_products(body) == [SearchHit("gid://shopify/Product/1", "Pack")]
    loose = mcp_body("Found gid://shopify/Product/7 and gid://shopify/Product/8")
    assert [h.product_gid for h in _mcp_products(loose)] == [
        "gid://shopify/Product/7",
        "gid://shopify/Product/8",
    ]
    with pytest.raises(ProbeUnavailable):
        _mcp_products({"jsonrpc": "2.0", "id": 1, "error": {"message": "unknown tool"}})


def test_retrieval_is_measured_against_answerable_products() -> None:
    result = QueryResult(
        "backpacks",
        "travel backpack",
        ["laptop_compartment"],
        "storefront_mcp",
        hits=[SearchHit("g1", "A"), SearchHit("g9", "Other"), SearchHit("g2", "B")],
    )
    measured = result.measure({"g1", "g2", "g3"}, {"g1": "A", "g3": "C"}, limit=10)
    assert measured["returned"] == 3 and measured["in_schema"] == 2
    assert measured["answerable_found"] == 1
    assert measured["recall"] == 0.5 and measured["precision"] == 0.33
    assert measured["missed_examples"] == ["C"]
    empty = QueryResult("tents", "q", ["x"], "admin_search").measure(set(), {}, 10)
    assert empty["recall"] is None and empty["precision"] is None


class RefusingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def paginate(self, *_: Any) -> Any:
        raise self.error
        yield  # pragma: no cover


async def test_category_metafields_are_skipped_when_shopify_refuses() -> None:
    attr = builtin_schemas()["backpacks"].attribute("material")
    assert attr is not None and attr.taxonomy_attribute
    request = TaxonomyRequest("s1", "gid://shopify/Product/1", "Pack", attr, "Nylon")
    denied = TaxonomyWriter(RefusingClient(ShopifyError("access denied")))  # type: ignore[arg-type]
    [item] = await denied.plan([request])
    assert item.action == "skipped" and "access denied" in (item.reason or "")
    revoked = TaxonomyWriter(RefusingClient(ShopifyAuthError("revoked")))  # type: ignore[arg-type]
    with pytest.raises(ShopifyAuthError):
        await revoked.plan([request])
