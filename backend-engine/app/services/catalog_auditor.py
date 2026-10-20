"""Catalog auditor: scores each product's readiness for AI shopping agents.

The AI Discovery Score (0-100) measures how completely a product exposes the structured data
agents rely on to find, compare and buy it. It is the sum of eight weighted dimensions:

    identity      15  title quality, brand, product type
    description   20  plain-text depth an LLM can quote and reason over
    media         15  images, alt text, multiple angles
    identifiers   15  SKU and valid GTIN on every variant
    offers        10  prices, inventory tracking, availability
    taxonomy      10  Shopify Standard Product Taxonomy category, tags
    attributes    10  named options, SEO metadata, attribute metafields
    reviews        5  rating metafields

`score_product` is pure and works on the Admin GraphQL product shape
(see `shopify_client.PRODUCT_FIELDS`). `CatalogAuditor.run` pages through a catalog and
persists the results.
"""

from __future__ import annotations

import hashlib
import html
import inspect
import json
import re
from collections import Counter
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services.tag_mapper import MappingRule, apply_rules

DIMENSION_WEIGHTS: dict[str, int] = {
    "identity": 15,
    "description": 20,
    "media": 15,
    "identifiers": 15,
    "offers": 10,
    "taxonomy": 10,
    "attributes": 10,
    "reviews": 5,
}
assert sum(DIMENSION_WEIGHTS.values()) == 100

# Human-readable guidance for each missing-field code (shown in the merchant dashboard).
ISSUE_MESSAGES: dict[str, str] = {
    "title_quality": "Title is too short, too long or written in all caps.",
    "brand_missing": "No brand (vendor) is set.",
    "product_type_missing": "No product type is set.",
    "description_missing": "Product has no description.",
    "description_short": "Description is too short for agents to answer shopper questions.",
    "images_missing": "Product has no images.",
    "image_alt_text_missing": "Some images have no alt text.",
    "few_images": "Fewer than 3 images.",
    "sku_missing": "Some variants have no SKU.",
    "gtin_missing": "Some variants have no barcode (GTIN/UPC/EAN).",
    "gtin_invalid": "Some barcodes are not valid GTINs (check digit or length).",
    "price_missing": "Some variants have no price.",
    "inventory_untracked": "Inventory is not tracked, so agents cannot confirm stock.",
    "unavailable": "No variant is available for sale.",
    "category_missing": "No Shopify Standard Product Taxonomy category is assigned.",
    "tags_sparse": "Fewer than 3 tags.",
    "options_unnamed": "Variant options use generic names.",
    "seo_title_missing": "No SEO title.",
    "seo_description_missing": "No SEO meta description.",
    "attributes_missing": "No attribute metafields (material, size chart, care, etc.).",
    "reviews_missing": "No product rating metafields (reviews.rating).",
}

NON_ATTRIBUTE_NAMESPACES = {
    "reviews",
    "global",
    "seo",
    "shopify--discovery--product_recommendation",
}
ATTRIBUTE_PROPERTIES = {"material", "color", "size", "pattern", "age_group", "gender", "care"}
GENERIC_OPTION_NAMES = {"title", "option", "option1", "option2", "option3", "default"}
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")


@dataclass
class ProductScore:
    product_gid: str
    title: str
    score: int
    breakdown: dict[str, dict[str, int]]
    missing_fields: list[str]
    content_hash: str
    mapped_attributes: dict[str, str] = field(default_factory=dict)
    # Filled by the catalog-intelligence assessor (services/readiness.py) when one is given.
    readiness: Any = None
    snapshot: dict[str, Any] = field(default_factory=dict)


def plain_text(description: str | None, description_html: str | None = None) -> str:
    text = description or TAG_RE.sub(" ", description_html or "")
    text = WS_RE.sub(" ", html.unescape(text)).strip()
    return SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)


def is_valid_gtin(code: str | None) -> bool:
    if not code:
        return False
    digits = code.strip().replace(" ", "").replace("-", "")
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def _nodes(product: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = product.get(key) or {}
    if isinstance(value, list):
        return value
    return value.get("nodes") or []


def content_hash(product: dict[str, Any], mapped: dict[str, str]) -> str:
    payload = {k: v for k, v in product.items() if k != "updatedAt"}
    raw = json.dumps([payload, mapped], sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def score_product(product: dict[str, Any], rules: list[MappingRule] | None = None) -> ProductScore:
    mapped = apply_rules(product, rules or [])
    missing: list[str] = []
    points: dict[str, int] = {}

    # identity (15)
    title = (product.get("title") or "").strip()
    pts = 0
    if 10 <= len(title) <= 150 and not (title.isupper() and len(title) > 12):
        pts += 5
    else:
        missing.append("title_quality")
    if (product.get("vendor") or "").strip() or mapped.get("brand"):
        pts += 5
    else:
        missing.append("brand_missing")
    if (product.get("productType") or "").strip():
        pts += 5
    else:
        missing.append("product_type_missing")
    points["identity"] = pts

    # description (20)
    text = plain_text(product.get("description"), product.get("descriptionHtml"))
    if text.lower() == title.lower():
        text = ""
    if len(text) >= 300:
        pts = 20
    elif len(text) >= 150:
        pts = 12
    elif len(text) >= 50:
        pts = 6
    else:
        pts = 0
    if not text:
        missing.append("description_missing")
    elif pts < 20:
        missing.append("description_short")
    points["description"] = pts

    # media (15)
    images = [m for m in _nodes(product, "media") if m.get("mediaContentType") == "IMAGE"]
    pts = 0
    if images:
        pts += 8
        if all(
            (m.get("alt") or (m.get("image") or {}).get("altText") or "").strip() for m in images
        ):
            pts += 4
        else:
            missing.append("image_alt_text_missing")
        if len(images) >= 3:
            pts += 3
        else:
            missing.append("few_images")
    else:
        missing.append("images_missing")
    points["media"] = pts

    # identifiers (15)
    variants = _nodes(product, "variants")
    pts = 0
    if variants and all((v.get("sku") or "").strip() for v in variants):
        pts += 5
    else:
        missing.append("sku_missing")
    barcodes = [(v.get("barcode") or "").strip() for v in variants]
    if mapped.get("gtin") and is_valid_gtin(mapped["gtin"]) and len(variants) <= 1:
        barcodes = [mapped["gtin"]]
    present = [b for b in barcodes if b]
    valid = [b for b in present if is_valid_gtin(b)]
    if barcodes and len(valid) == len(barcodes):
        pts += 10
    elif valid:
        pts += 5
    if len(present) < len(barcodes) or not barcodes:
        missing.append("gtin_missing")
    if len(valid) < len(present):
        missing.append("gtin_invalid")
    points["identifiers"] = pts

    # offers (10)
    pts = 0
    if variants and all(_positive(v.get("price")) for v in variants):
        pts += 5
    else:
        missing.append("price_missing")
    if variants and all((v.get("inventoryItem") or {}).get("tracked") for v in variants):
        pts += 3
    else:
        missing.append("inventory_untracked")
    if any(v.get("availableForSale") for v in variants):
        pts += 2
    else:
        missing.append("unavailable")
    points["offers"] = pts

    # taxonomy (10)
    pts = 0
    if (product.get("category") or {}).get("id"):
        pts += 7
    else:
        missing.append("category_missing")
    if len(product.get("tags") or []) >= 3:
        pts += 3
    else:
        missing.append("tags_sparse")
    points["taxonomy"] = pts

    # attributes (10)
    pts = 0
    options = product.get("options") or []
    single_default = len(variants) <= 1
    if single_default or all(
        (o.get("name") or "").strip().lower() not in GENERIC_OPTION_NAMES for o in options
    ):
        pts += 3
    else:
        missing.append("options_unnamed")
    seo = product.get("seo") or {}
    if (seo.get("title") or "").strip():
        pts += 2
    else:
        missing.append("seo_title_missing")
    if (seo.get("description") or "").strip():
        pts += 2
    else:
        missing.append("seo_description_missing")
    metafields = _nodes(product, "metafields")
    has_attr_metafield = any(
        m.get("namespace") not in NON_ATTRIBUTE_NAMESPACES and (m.get("value") or "").strip()
        for m in metafields
    )
    if has_attr_metafield or ATTRIBUTE_PROPERTIES & mapped.keys():
        pts += 3
    else:
        missing.append("attributes_missing")
    points["attributes"] = pts

    # reviews (5)
    has_rating = (
        any(
            m.get("namespace") == "reviews" and m.get("key") in ("rating", "rating_count")
            for m in metafields
        )
        or "rating" in mapped
    )
    if has_rating:
        points["reviews"] = 5
    else:
        points["reviews"] = 0
        missing.append("reviews_missing")

    breakdown = {
        dim: {"points": points[dim], "max": weight} for dim, weight in DIMENSION_WEIGHTS.items()
    }
    return ProductScore(
        product_gid=str(product.get("id")),
        title=title,
        score=sum(points.values()),
        breakdown=breakdown,
        missing_fields=missing,
        content_hash=content_hash(product, mapped),
        mapped_attributes=mapped,
    )


def _positive(value: Any) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (ArithmeticError, ValueError):
        return False


@dataclass
class CatalogSummary:
    products_scanned: int
    avg_score: Decimal | None
    grade_counts: dict[str, int]
    top_issues: list[tuple[str, int]]
    avg_readiness: Decimal | None = None


def grade(score: int) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 50:
        return "needs_work"
    return "poor"


def summarize(scores: list[ProductScore]) -> CatalogSummary:
    if not scores:
        return CatalogSummary(0, None, {}, [])
    avg = (Decimal(sum(s.score for s in scores)) / len(scores)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    grades = Counter(grade(s.score) for s in scores)
    issues = Counter(code for s in scores for code in s.missing_fields)
    readiness = [s.readiness.score for s in scores if s.readiness is not None]
    avg_readiness = (
        (Decimal(sum(readiness)) / len(readiness)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if readiness
        else None
    )
    return CatalogSummary(len(scores), avg, dict(grades), issues.most_common(10), avg_readiness)


class CatalogAuditor:
    """Streams products, scores them and hands results to a sink in batches."""

    def __init__(
        self,
        rules: list[MappingRule],
        batch_size: int = 100,
        assessor: Callable[[dict[str, Any], ProductScore], None] | None = None,
    ) -> None:
        self._rules = rules
        self._batch_size = batch_size
        self._assessor = assessor

    async def run(
        self,
        products: AsyncIterable[dict[str, Any]],
        sink: Callable[[list[ProductScore]], Awaitable[None] | None],
    ) -> CatalogSummary:
        batch: list[ProductScore] = []
        all_scores: list[ProductScore] = []
        async for product in products:
            scored = score_product(product, self._rules)
            if self._assessor is not None:
                self._assessor(product, scored)
            batch.append(scored)
            all_scores.append(scored)
            if len(batch) >= self._batch_size:
                await _drain(sink(batch))
                batch = []
        if batch:
            await _drain(sink(batch))
        return summarize(all_scores)


async def _drain(result: Awaitable[None] | None) -> None:
    if inspect.isawaitable(result):
        await result
