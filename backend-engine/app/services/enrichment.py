"""Turn a product's content into reviewable attribute suggestions.

For each schema attribute, a value can come from the deterministic extractor or from the AI
model. Either way it is validated here before it is stored:

1. the value must parse as the attribute's type (`catalog_schema.parse_value`);
2. it needs evidence, and the evidence must occur in the product's own content;
3. the evidence must state the value: numbers in the value appear in the quote, vocabulary
   terms appear in the quote.

A value that fails is not stored as a proposal. The suggestion records the attribute as unknown
with the reason, so the merchant can supply the value instead. Nothing here writes to Shopify.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.ai_enricher import AIAttribute, AIEnrichment
from app.services.attribute_extractor import (
    INSTRUCTION_RE,
    CurrentValue,
    TextSource,
    current_attributes,
    extract_from_text,
    text_sources,
)
from app.services.catalog_schema import (
    MEASUREMENT_TYPES,
    AttributeDefinition,
    CategorySchema,
    InvalidValue,
    display_value,
    match_schema,
    parse_value,
    validate_value,
    values_equal,
)
from app.services.readiness import Finding

QUOTE_CHARS: dict[str, str | int | None] = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "–": "-",
    "—": "-",
}
NUM_TOKEN_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
QUOTE_SPLIT_RE = re.compile(r"\s*(?:…|\.\.\.)\s*")


@dataclass
class SuggestionDraft:
    """A validated attribute suggestion ready to be stored for review."""

    attribute_key: str
    data_type: str
    current_value: Any
    proposed_value: Any
    evidence: str | None
    evidence_source: str | None
    confidence: Decimal
    method: str
    explanation: str
    validation_status: str  # valid | warning | invalid | unknown
    validation_errors: list[str] = field(default_factory=list)
    rule_confirmed: bool = False


@dataclass
class EnrichmentResult:
    """A product's suggestions and findings, plus its current attribute values."""

    schema_key: str
    suggestions: list[SuggestionDraft]
    findings: list[Finding]
    attributes: dict[str, CurrentValue]


def normalize(text: str) -> str:
    """Normalize text for evidence matching: NFKC, lowercase, plain quotes, single spaces."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.translate(str.maketrans(QUOTE_CHARS))
    return re.sub(r"\s+", " ", text).strip()


def _haystack(sources: list[TextSource], metafields: list[dict[str, Any]]) -> str:
    parts = [s.text for s in sources] + [str(m.get("value") or "") for m in metafields]
    return normalize(" \n ".join(parts))


def evidence_found(evidence: str, haystack: str) -> bool:
    """Whether every part of a quote (split on ellipses) appears in the normalized text."""
    parts = [normalize(p) for p in QUOTE_SPLIT_RE.split(evidence) if p.strip()]
    return bool(parts) and all(p.strip(" .,;:") in haystack for p in parts)


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(n.replace(",", ".")) for n in NUM_TOKEN_RE.findall(text)}


def validate_proposal(
    attr: AttributeDefinition,
    raw_value: Any,
    evidence: str | None,
    haystack: str,
) -> tuple[Any, str, list[str]]:
    """(canonical value or None, validation_status, errors) for one proposed value."""
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return None, "unknown", []
    if not evidence or not evidence.strip():
        return None, "invalid", ["no evidence quoted from the product content"]
    if not evidence_found(evidence, haystack):
        return None, "invalid", ["the quoted evidence does not appear in the product content"]
    if INSTRUCTION_RE.search(evidence):
        return None, "invalid", ["the evidence reads as an instruction, not a product fact"]
    try:
        value = parse_value(raw_value, attr)
    except InvalidValue as exc:
        return None, "invalid", [f"value is not a valid {attr.type}: {exc}"]

    quote = normalize(evidence)
    if attr.type in MEASUREMENT_TYPES or attr.type == "number":
        number = Decimal(str(value["value"] if isinstance(value, dict) else value))
        if number not in _numbers(quote):
            return None, "invalid", [f"the evidence does not state the number {number}"]
    elif attr.type in ("text", "list") and attr.allowed_values:
        items = value if isinstance(value, list) else [value]
        stated = [i for i in items if normalize(i) in quote or _any_term_in(i, quote)]
        if not stated:
            return None, "invalid", ["the evidence does not mention the proposed value"]
        if attr.type == "list":
            value = stated
    errors = validate_value(value, attr)
    return value, ("warning" if errors else "valid"), errors


def _any_term_in(item: str, quote: str) -> bool:
    # "Ripstop nylon / polyester": accept when each part is in the quote.
    parts = [normalize(p) for p in re.split(r"\s*/\s*", item) if p.strip()]
    return bool(parts) and all(p in quote for p in parts)


def _metafields(product: dict[str, Any]) -> list[dict[str, Any]]:
    value = product.get("metafields") or {}
    return value if isinstance(value, list) else value.get("nodes") or []


def build_suggestions(
    product: dict[str, Any],
    schemas: dict[str, CategorySchema],
    mappings: dict[str, tuple[str, str]],
    ai: AIEnrichment | None = None,
    ai_model: str | None = None,
    ai_error: str | None = None,
) -> EnrichmentResult:
    """Build validated suggestions and findings for a product from rule extraction and, if given,
    the AI result."""
    schema = match_schema(product, schemas)
    sources = text_sources(product)
    metafields = _metafields(product)
    haystack = _haystack(sources, metafields)
    current = current_attributes(product, schema, mappings)
    ai_by_key: dict[str, AIAttribute] = {a.key: a for a in (ai.attributes if ai else [])}
    ai_method = f"llm:{ai_model}" if ai_model else "llm"

    drafts: list[SuggestionDraft] = []
    for attr in schema.attributes:
        cur = current.get(attr.key)
        cur_value = cur.value if cur and not cur.errors else None
        rule = extract_from_text(attr, sources)
        candidates: list[tuple[str, Any, str | None, str | None, float, str]] = []
        rejected: list[str] = []
        rule_value: Any = None

        ai_attr = ai_by_key.get(attr.key)
        if ai_attr is not None and ai_attr.status == "stated":
            value, status, errors = validate_proposal(
                attr, ai_attr.value, ai_attr.evidence_quote, haystack
            )
            if value is not None:
                candidates.append(
                    (
                        ai_method,
                        value,
                        ai_attr.evidence_quote,
                        ai_attr.evidence_source,
                        _clamp(ai_attr.confidence),
                        ai_attr.explanation,
                    )
                )
            else:
                rejected.append(f"AI proposed {ai_attr.value!r}, rejected: {'; '.join(errors)}")
        if rule is not None:
            value, status, errors = validate_proposal(attr, rule.value, rule.evidence, haystack)
            if value is not None:
                rule_value = value
                candidates.append(
                    (
                        rule.method,
                        value,
                        rule.evidence,
                        rule.source,
                        rule.confidence,
                        f"Stated in the product {rule.source}.",
                    )
                )

        if not candidates:
            if cur_value is not None:
                continue  # already structured; nothing new to propose
            if cur is None and not (attr.required or attr.importance in ("critical", "high")):
                continue
            reasons = rejected or ([f"AI enrichment unavailable: {ai_error}"] if ai_error else [])
            explanation = (
                f"The current value {cur.raw!r} is invalid ({'; '.join(cur.errors)}) and the "
                "product content does not state a correct one."
                if cur is not None
                else f"The product content does not state the {attr.label.lower()}."
            )
            drafts.append(
                SuggestionDraft(
                    attribute_key=attr.key,
                    data_type=attr.type,
                    current_value=cur.raw if cur else None,
                    proposed_value=None,
                    evidence=None,
                    evidence_source=None,
                    confidence=Decimal("0"),
                    method=ai_method if ai is not None else "rule_extraction",
                    explanation=explanation,
                    validation_status="unknown",
                    validation_errors=reasons,
                )
            )
            continue

        method, value, evidence, source, confidence, explanation = candidates[0]
        errors = validate_value(value, attr)
        status = "warning" if errors else "valid"
        for other in candidates[1:]:
            if values_equal(other[1], value, attr):
                confidence = max(confidence, other[4])
                explanation += " Confirmed by rule-based extraction."
            else:
                status = "warning"
                errors.append(f"rule-based extraction read {display_value(other[1], attr)} instead")
                confidence = min(confidence, 0.5)
        if cur_value is not None and values_equal(cur_value, value, attr):
            continue  # the product already has this value
        if cur_value is not None:
            errors.append(
                f"differs from the current value {display_value(cur_value, attr)}; approving "
                "replaces it"
            )
            status = "warning"
        drafts.append(
            SuggestionDraft(
                attribute_key=attr.key,
                data_type=attr.type,
                current_value=cur.raw if cur else None,
                proposed_value=value,
                evidence=evidence,
                evidence_source=source,
                confidence=Decimal(str(round(confidence, 2))),
                method=method,
                explanation=explanation[:1000],
                validation_status=status,
                validation_errors=errors + rejected,
                rule_confirmed=rule_value is not None and values_equal(rule_value, value, attr),
            )
        )

    findings = [_ai_finding(issue, haystack, schema) for issue in (ai.issues if ai else [])]
    return EnrichmentResult(schema.key, drafts, findings, current)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _ai_finding(issue: Any, haystack: str, schema: CategorySchema) -> Finding:
    verified = bool(issue.evidence_quote) and evidence_found(issue.evidence_quote, haystack)
    confidence = _clamp(issue.confidence) if verified else min(_clamp(issue.confidence), 0.5)
    attribute = (
        issue.attribute_key
        if issue.attribute_key and schema.attribute(issue.attribute_key)
        else None
    )
    return Finding(
        code=f"ai_{issue.kind}",
        severity=issue.severity,
        attribute_key=attribute,
        message=issue.message[:1000],
        why_it_matters=(
            "AI-assisted review: agents may misread or skip products whose data is ambiguous "
            "or contradictory."
        ),
        recommended_action="Review the product content and clarify or correct it.",
        evidence=issue.evidence_quote[:500] if verified and issue.evidence_quote else None,
        confidence=Decimal(str(round(confidence, 2))),
        method="ai",
    )
