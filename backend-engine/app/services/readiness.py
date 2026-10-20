"""Catalog readiness: deterministic audit findings and a transparent per-product score.

The readiness score (0-100) measures how well a product's *structured* data supports AI
shopping agents, against the category schema it matches. It is an internal data-quality
measure, not any platform's ranking or visibility score. Each component scores 0-1, is
multiplied by its weight, and components that do not apply to a product (for example
consistency, when nothing structured can be compared with the description) are left out and
the remaining weights are rescaled:

    score = 100 * sum(weight_c * value_c) / sum(weight_c)      over applicable components

    completeness   30  importance-weighted share of schema attributes with a valid structured
                       value (critical 4, high 3, medium 2, low 1)
    validity       15  share of present structured values that parse and fit the schema
    consistency    15  share of structured values that agree with what the description says
    evidence       10  share of product claims (waterproof, recycled, ...) backed by a field
    variants       10  variant checks: SKUs, prices, named options, expected options (Size)
    foundation     20  the AI Discovery Score (description, media, identifiers, taxonomy)

Weights are configurable per merchant (`DEFAULT_WEIGHTS` are the defaults). Every lost point
traces back to a finding, so merchants can see exactly what to fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.attribute_extractor import (
    CurrentValue,
    Extraction,
    current_attributes,
    extract_from_text,
    text_sources,
)
from app.services.catalog_auditor import ISSUE_MESSAGES, ProductScore, plain_text
from app.services.catalog_schema import (
    IMPORTANCE_WEIGHT,
    CategorySchema,
    display_value,
    match_schema,
    values_equal,
)

DEFAULT_WEIGHTS: dict[str, int] = {
    "completeness": 30,
    "validity": 15,
    "consistency": 15,
    "evidence": 10,
    "variants": 10,
    "foundation": 20,
}
READY_THRESHOLD = 80

SEVERITY_FOR_IMPORTANCE = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}

# Claims that should be backed by a structured field. Each maps to the attribute keys that
# substantiate it; a claim with none of them present (or none in the schema) is unsupported.
CLAIMS: dict[str, tuple[str, list[str]]] = {
    "waterproof": (r"\bwaterproof\b", ["water_resistance", "waterproof_rating"]),
    "water_resistant": (r"\bwater[- ]?(?:resistant|repellent)\b", ["water_resistance"]),
    "ultralight": (r"\bultra[- ]?light(?:weight)?\b", ["weight"]),
    "lightweight": (r"\blight[- ]?weight\b", ["weight"]),
    "recycled": (r"\brecycled\b", ["material"]),
    "certified": (r"\b(?:certified|bluesign|fair trade|oeko-tex)\b", ["certifications"]),
    "warranty": (r"\b(?:lifetime|year) warranty\b", ["warranty"]),
}
VAGUE_TERMS = re.compile(
    r"\b(versatile|spacious|premium|high[- ]quality|perfect for|ultimate|amazing"
    r"|best[- ]in[- ]class)\b",
    re.IGNORECASE,
)

# Severity and guidance for the foundational (AI Discovery Score) issue codes.
FOUNDATION_SEVERITY: dict[str, str] = {
    "description_missing": "critical",
    "price_missing": "critical",
    "images_missing": "high",
    "description_short": "medium",
    "category_missing": "medium",
    "gtin_missing": "medium",
    "gtin_invalid": "high",
    "sku_missing": "medium",
    "brand_missing": "medium",
    "product_type_missing": "medium",
    "options_unnamed": "medium",
    "title_quality": "low",
    "image_alt_text_missing": "low",
    "few_images": "low",
    "inventory_untracked": "low",
    "unavailable": "low",
    "tags_sparse": "low",
    "seo_title_missing": "low",
    "seo_description_missing": "low",
    "attributes_missing": "low",
    "reviews_missing": "low",
}
FOUNDATION_WHY: dict[str, str] = {
    "description": "Agents quote and reason over the description to answer shopper questions.",
    "images": "Agents and shopping surfaces show images and use alt text to describe products.",
    "identifiers": "GTINs and SKUs let agents match the same product across sellers.",
    "price": "Agents cannot recommend a product they cannot price.",
    "taxonomy": "A Shopify taxonomy category tells agents what kind of product this is.",
    "other": "Complete product data helps agents understand and compare the product.",
}


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    why_it_matters: str
    recommended_action: str
    attribute_key: str | None = None
    evidence: str | None = None
    confidence: Decimal | None = None
    method: str = "rule"


@dataclass
class ProductReadiness:
    schema_key: str
    score: int
    breakdown: dict[str, dict[str, Any]]
    findings: list[Finding]
    attributes: dict[str, CurrentValue]
    extractions: dict[str, Extraction] = field(default_factory=dict)

    def attributes_json(self) -> dict[str, Any]:
        return {k: v.as_json() for k, v in self.attributes.items()}

    def supports(self, keys: list[str]) -> bool:
        return all(
            k in self.attributes and self.attributes[k].value is not None
            and not self.attributes[k].errors
            for k in keys
        )  # fmt: skip


def normalize_weights(raw: dict[str, Any] | None) -> dict[str, int]:
    weights = dict(DEFAULT_WEIGHTS)
    for key, value in (raw or {}).items():
        if key in weights and isinstance(value, int) and 0 <= value <= 100:
            weights[key] = value
    return weights


def _foundation_why(code: str) -> str:
    for prefix in ("description", "images", "image", "gtin", "sku", "price", "category"):
        if code.startswith(prefix):
            return FOUNDATION_WHY.get(
                {"image": "images", "gtin": "identifiers", "sku": "identifiers",
                 "category": "taxonomy"}.get(prefix, prefix),
                FOUNDATION_WHY["other"],
            )  # fmt: skip
    return FOUNDATION_WHY["other"]


def assess_product(
    product: dict[str, Any],
    base: ProductScore,
    schemas: dict[str, CategorySchema],
    mappings: dict[str, tuple[str, str]],
    weights: dict[str, int] | None = None,
) -> ProductReadiness:
    schema = match_schema(product, schemas)
    weights = normalize_weights(weights)
    current = current_attributes(product, schema, mappings, base.mapped_attributes)
    sources = text_sources(product)
    findings: list[Finding] = []
    extractions: dict[str, Extraction] = {}

    total_weight = earned = 0
    present = valid = compared = agreeing = 0
    for attr in schema.attributes:
        importance = attr.importance if attr.required else "low"
        weight = IMPORTANCE_WEIGHT[attr.importance]
        total_weight += weight
        cur = current.get(attr.key)
        extraction = extract_from_text(attr, sources)
        if extraction is not None:
            extractions[attr.key] = extraction

        if cur is None:
            if extraction is not None:
                findings.append(
                    Finding(
                        code="attribute_buried",
                        severity="medium" if importance in ("low", "medium") else "high",
                        attribute_key=attr.key,
                        message=(
                            f"{attr.label} is stated in the {extraction.source} "
                            f"({display_value(extraction.value, attr)}) but not stored as a "
                            "structured field."
                        ),
                        why_it_matters=attr.why
                        or "Agents filter on structured fields, not on prose.",
                        recommended_action="Run enrichment to propose it as a structured value.",
                        evidence=extraction.evidence,
                        confidence=Decimal(str(extraction.confidence)),
                    )
                )
            elif attr.required or attr.importance in ("critical", "high"):
                findings.append(
                    Finding(
                        code="attribute_missing",
                        severity=SEVERITY_FOR_IMPORTANCE[importance],
                        attribute_key=attr.key,
                        message=f"{attr.label} is missing and not stated anywhere in the listing.",
                        why_it_matters=attr.why or "Agents cannot answer questions about it.",
                        recommended_action=(
                            f"Add the {attr.label.lower()} to the product (enrichment will mark "
                            "it unknown so you can fill it in)."
                        ),
                    )
                )
            continue

        present += 1
        if cur.errors:
            findings.append(
                Finding(
                    code="attribute_invalid",
                    severity="high",
                    attribute_key=attr.key,
                    message=(
                        f"{attr.label} value {cur.raw!r} is not valid: {'; '.join(cur.errors)}."
                    ),
                    why_it_matters="Invalid values are ignored or misread by agents.",
                    recommended_action=f"Correct the value in {cur.source.split(':', 1)[-1]}.",
                    evidence=str(cur.raw)[:300],
                )
            )
            continue
        valid += 1
        earned += weight
        if extraction is not None and attr.type not in ("text", "list"):
            compared += 1
            if values_equal(cur.value, extraction.value, attr):
                agreeing += 1
            else:
                findings.append(
                    Finding(
                        code="attribute_contradiction",
                        severity="high",
                        attribute_key=attr.key,
                        message=(
                            f"{attr.label} is {display_value(cur.value, attr)} in "
                            f"{cur.source}, but the {extraction.source} says "
                            f"{display_value(extraction.value, attr)}."
                        ),
                        why_it_matters="Contradictions make agents distrust or skip a product.",
                        recommended_action="Decide which value is right and fix the other.",
                        evidence=extraction.evidence,
                        confidence=Decimal(str(extraction.confidence)),
                    )
                )

    # Claims stated in text that no structured field substantiates.
    full_text = " ".join(s.text for s in sources)
    claims = unsupported = 0
    for pattern, keys in CLAIMS.values():
        match = re.search(pattern, full_text, re.IGNORECASE)
        if not match:
            continue
        claims += 1
        if any(k in current and not current[k].errors for k in keys):
            continue
        unsupported += 1
        sentence = _sentence_around(full_text, match.start())
        findings.append(
            Finding(
                code="claim_unsupported",
                severity="medium",
                attribute_key=next((k for k in keys if schema.attribute(k)), None),
                message=f"The listing claims '{match.group(0)}' but no structured field backs it.",
                why_it_matters=(
                    "Agents favour claims they can verify; unbacked claims can be ignored or "
                    "flagged as unreliable."
                ),
                recommended_action=(
                    "Add the supporting attribute (rating, material, certification) or remove "
                    "the claim if it cannot be substantiated."
                ),
                evidence=sentence,
            )
        )

    # Vague description: marketing words but few concrete facts.
    description = plain_text(product.get("description"), product.get("descriptionHtml"))
    if (
        description
        and len(VAGUE_TERMS.findall(description)) >= 2
        and not re.search(r"\d", description)
    ):
        findings.append(
            Finding(
                code="description_vague",
                severity="medium",
                message="The description relies on marketing language and states no figures.",
                why_it_matters="Agents need concrete specifications to compare products.",
                recommended_action="Add measurable facts: dimensions, weight, capacity, materials.",
                evidence=description[:300],
            )
        )

    # Variants.
    variants = _nodes(product, "variants")
    option_names = {(o.get("name") or "").strip().lower() for o in product.get("options") or []}
    variant_checks = variant_ok = 0
    if variants:
        variant_checks += 2
        variant_ok += all((v.get("sku") or "").strip() for v in variants)
        variant_ok += all(_positive(v.get("price")) for v in variants)
        if len(variants) > 1:
            for expected in schema.variant_options:
                variant_checks += 1
                if expected.lower() in option_names:
                    variant_ok += 1
                else:
                    findings.append(
                        Finding(
                            code="variant_option_missing",
                            severity="high",
                            message=(
                                f"Variants are not described by a '{expected}' option "
                                f"(options: {', '.join(sorted(option_names)) or 'none'})."
                            ),
                            why_it_matters=(
                                f"Agents must pick the right {expected.lower()} to add to a cart."
                            ),
                            recommended_action=f"Name the variant option '{expected}'.",
                        )
                    )

    for code in base.missing_fields:
        findings.append(
            Finding(
                code=code,
                severity=FOUNDATION_SEVERITY.get(code, "low"),
                message=ISSUE_MESSAGES.get(code, code),
                why_it_matters=_foundation_why(code),
                recommended_action="Fix this in the Shopify product editor.",
            )
        )

    components: dict[str, float | None] = {
        "completeness": earned / total_weight if total_weight else None,
        "validity": valid / present if present else None,
        "consistency": agreeing / compared if compared else None,
        "evidence": (claims - unsupported) / claims if claims else None,
        "variants": variant_ok / variant_checks if variant_checks else None,
        "foundation": base.score / 100,
    }
    breakdown: dict[str, dict[str, Any]] = {}
    num = den = 0.0
    for name, value in components.items():
        w = weights[name]
        breakdown[name] = {
            "weight": w,
            "value": None if value is None else round(value, 3),
            "applicable": value is not None,
            "points": None if value is None else round(value * w, 1),
        }
        if value is not None and w:
            num += value * w
            den += w
    score = round(100 * num / den) if den else 0
    return ProductReadiness(
        schema_key=schema.key,
        score=score,
        breakdown=breakdown,
        findings=findings,
        attributes=current,
        extractions=extractions,
    )


def _sentence_around(text: str, index: int) -> str:
    start = max(text.rfind(". ", 0, index) + 2, 0)
    end = text.find(". ", index)
    return text[start : end + 1 if end != -1 else len(text)][:300].strip()


def _nodes(product: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = product.get(key) or {}
    return value if isinstance(value, list) else value.get("nodes") or []


def _positive(value: Any) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (ArithmeticError, ValueError):
        return False
