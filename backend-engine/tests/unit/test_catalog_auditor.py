import copy
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services.catalog_auditor import (
    DIMENSION_WEIGHTS,
    CatalogAuditor,
    ProductScore,
    grade,
    is_valid_gtin,
    plain_text,
    score_product,
    summarize,
)
from app.services.tag_mapper import MappingRule, apply_rules, preview_rule

PRODUCTS = json.loads((Path(__file__).parent.parent / "fixtures/shopify/products.json").read_text())


def product(name: str) -> dict[str, Any]:
    return copy.deepcopy(PRODUCTS[name])


@pytest.mark.parametrize(
    "code,valid",
    [
        ("4006381333931", True),  # EAN-13
        ("012345678905", True),  # UPC-A
        ("73513537", True),  # EAN-8
        ("00012345678905", True),  # GTIN-14
        ("4006381333932", False),  # bad check digit
        ("123", False),
        ("ABC123456789", False),
        (None, False),
    ],
)
def test_gtin_validation(code: str | None, valid: bool) -> None:
    assert is_valid_gtin(code) is valid


def test_weights_sum_to_100() -> None:
    assert sum(DIMENSION_WEIGHTS.values()) == 100


def test_complete_product_scores_100() -> None:
    result = score_product(product("complete"))
    assert result.score == 100
    assert result.missing_fields == []
    assert result.breakdown["description"] == {"points": 20, "max": 20}


def test_sparse_product_flags_everything_actionable() -> None:
    result = score_product(product("sparse"))
    assert result.score == 10
    assert {
        "title_quality",
        "brand_missing",
        "description_missing",
        "images_missing",
        "sku_missing",
        "gtin_invalid",
        "category_missing",
        "reviews_missing",
    } <= set(result.missing_fields)
    assert sum(d["points"] for d in result.breakdown.values()) == result.score


def test_partial_product() -> None:
    result = score_product(product("partial"))
    assert result.score == 47
    assert "description_short" in result.missing_fields
    assert "image_alt_text_missing" in result.missing_fields
    assert "gtin_missing" in result.missing_fields


def test_description_equal_to_title_does_not_count() -> None:
    p = product("partial")
    p["description"] = p["title"]
    assert "description_missing" in score_product(p).missing_fields


def test_html_only_description_is_used() -> None:
    assert (
        plain_text(None, "<p>Soft &amp; warm</p><ul><li>Merino</li></ul>") == "Soft & warm Merino"
    )


def test_mapping_rules_fill_gaps() -> None:
    p = product("partial")
    p["vendor"] = ""
    p["tags"] = ["brand:acme-trail", "gtin:4006381333931", "material:merino-wool"]
    rules = [
        MappingRule("tag", r"brand:(.+)", "brand", transform={"case": "title"}),
        MappingRule("tag", r"gtin:(\d+)", "gtin"),
        MappingRule("tag", r"material:(.+)", "material"),
    ]
    before = score_product(p)
    after = score_product(p, rules)
    assert after.mapped_attributes == {
        "brand": "Acme Trail",
        "gtin": "4006381333931",
        "material": "merino wool",
    }
    assert "brand_missing" in before.missing_fields and "brand_missing" not in after.missing_fields
    assert "gtin_missing" not in after.missing_fields
    assert "attributes_missing" not in after.missing_fields
    assert after.score > before.score
    assert after.content_hash != before.content_hash


def test_mapping_rule_priority_and_metafield_source() -> None:
    p = product("complete")
    rules = [
        MappingRule("metafield", r"(.+)", "care", source_key="custom.care", priority=10),
        MappingRule("vendor", r".*", "brand", transform={"value": "Override"}, priority=50),
        MappingRule("vendor", r"(.+)", "brand", priority=5),
    ]
    assert apply_rules(p, rules) == {"care": "Machine wash cold", "brand": "Acme Outfitters"}


def test_preview_rule_reports_matches_unmatched_values_and_overrides() -> None:
    products = [
        {"id": "P1", "title": "Pack", "tags": ["material:ripstop-nylon", "sale"]},
        {"id": "P2", "title": "Tent", "tags": ["Material:polyester"]},
        {"id": "P3", "title": "Hat", "tags": ["Sale", "wool"]},
    ]
    rule = MappingRule("tag", r"material:(.+)", "material", transform={"case": "title"})
    stronger = MappingRule("tag", r"material:polyester", "material", priority=1)
    weaker = MappingRule("tag", r"(.+)", "material", priority=500)
    out = preview_rule(rule, products, [stronger, weaker])
    assert out["products_scanned"] == 3
    assert out["matched"] == 2
    rows = [
        (m["title"], m["source_value"], m["value"], m["overridden_by_rule"]) for m in out["matches"]
    ]
    assert rows == [
        ("Pack", "material:ripstop-nylon", "Ripstop Nylon", False),
        ("Tent", "Material:polyester", "Polyester", True),
    ]
    assert out["unmatched_values"] == ["sale", "wool"]  # case-insensitive de-duplication


def test_content_hash_ignores_updated_at() -> None:
    a, b = product("complete"), product("complete")
    b["updatedAt"] = "2030-01-01T00:00:00Z"
    assert score_product(a).content_hash == score_product(b).content_hash


def test_grades_and_summary() -> None:
    assert [grade(s) for s in (100, 85, 84, 70, 69, 50, 49)] == [
        "excellent",
        "excellent",
        "good",
        "good",
        "needs_work",
        "needs_work",
        "poor",
    ]
    scores = [score_product(product(n)) for n in ("complete", "sparse", "partial")]
    summary = summarize(scores)
    assert summary.products_scanned == 3
    assert summary.avg_score == Decimal("52.33")
    assert summary.grade_counts == {"excellent": 1, "poor": 2}
    assert dict(summary.top_issues)["reviews_missing"] == 2
    assert summarize([]).avg_score is None


async def _aiter(items: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for item in items:
        yield item


async def test_auditor_batches_results() -> None:
    batches: list[list[ProductScore]] = []
    items = [product("complete"), product("sparse"), product("partial")]
    summary = await CatalogAuditor([], batch_size=2).run(_aiter(items), batches.append)
    assert [len(b) for b in batches] == [2, 1]
    assert summary.products_scanned == 3
