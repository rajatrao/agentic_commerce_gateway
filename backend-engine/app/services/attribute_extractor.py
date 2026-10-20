"""Read a product's structured attributes, and find facts stated only in its text.

`current_attributes` reads what is already structured: the mapped metafield, a metafield with
the attribute's key, a merchant meta-mapping rule, or a product option. `extract_from_text`
is the deterministic extractor: it only returns a value when a sentence in the product's own
title, description, tags or options states it, and that sentence is kept as evidence. It never
infers a value that is not written down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.catalog_auditor import plain_text
from app.services.catalog_schema import (
    MEASUREMENT_TYPES,
    NUMBER_RE,
    UNIT_ALIASES,
    AttributeDefinition,
    CategorySchema,
    InvalidValue,
    parse_measurement,
    parse_value,
    validate_value,
)

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+|\s+[•·|]\s+")
# Text addressed to an AI system rather than describing the product. A value whose only
# evidence is such a sentence is rejected even when the sentence contains the value.
INSTRUCTION_RE = re.compile(
    r"\b(ignore (?:all |any )?(?:previous|prior|above)|disregard (?:all|any|previous|the)"
    r"|system prompt|(?:previous|prior|these|following|new) instructions"
    r"|you (?:must|should|are an?)\b|language model"
    r"|(?:report|output|answer|respond|classify)\b[^.]{0,40}\bas\b)",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(r"\b(no|not|without|lacks?|isn't|doesn't|does not)\b", re.IGNORECASE)


@dataclass
class TextSource:
    name: str  # title | description | tags | options
    text: str


@dataclass
class CurrentValue:
    value: Any
    raw: Any
    source: str
    errors: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {"value": self.value, "raw": self.raw, "source": self.source, "errors": self.errors}


@dataclass
class Extraction:
    value: Any
    evidence: str
    source: str
    confidence: float
    method: str = "rule_extraction"


def text_sources(product: dict[str, Any]) -> list[TextSource]:
    options = "; ".join(
        f"{o.get('name')}: {', '.join(str(v) for v in o.get('values') or [])}"
        for o in product.get("options") or []
        if (o.get("name") or "").lower() != "title"
    )
    return [
        TextSource("title", product.get("title") or ""),
        TextSource(
            "description", plain_text(product.get("description"), product.get("descriptionHtml"))
        ),
        TextSource("tags", ", ".join(product.get("tags") or [])),
        TextSource("options", options),
    ]


def _metafields(product: dict[str, Any]) -> list[dict[str, Any]]:
    value = product.get("metafields") or {}
    return value if isinstance(value, list) else value.get("nodes") or []


def current_attributes(
    product: dict[str, Any],
    schema: CategorySchema,
    mappings: dict[str, tuple[str, str]],
    mapped_rules: dict[str, str] | None = None,
) -> dict[str, CurrentValue]:
    """Structured values already on the product, by attribute key."""
    metafields = {
        (m.get("namespace"), m.get("key")): m
        for m in _metafields(product)
        if (m.get("value") or "").strip()
    }
    by_key = {key: m for (_, key), m in metafields.items()}
    options = {
        (o.get("name") or "").strip().lower(): o.get("values") or []
        for o in product.get("options") or []
    }
    out: dict[str, CurrentValue] = {}
    for attr in schema.attributes:
        target = mappings.get(attr.key) or (
            (attr.metafield.namespace, attr.metafield.key) if attr.metafield else None
        )
        candidates: list[tuple[str, Any]] = []
        if target and target in metafields:
            candidates.append((f"metafield:{target[0]}.{target[1]}", metafields[target]["value"]))
        elif attr.key in by_key:
            m = by_key[attr.key]
            candidates.append((f"metafield:{m['namespace']}.{m['key']}", m["value"]))
        if mapped_rules and attr.key in mapped_rules:
            candidates.append(("mapping_rule", mapped_rules[attr.key]))
        for name in (attr.key.replace("_", " "), attr.label.lower()):
            values = options.get(name)
            if values and len(values) == 1:
                candidates.append((f"option:{name}", values[0]))
        if not candidates:
            continue
        source, raw = candidates[0]
        try:
            value = parse_value(raw, attr)
            errors = validate_value(value, attr)
        except InvalidValue as exc:
            value, errors = None, [str(exc)]
        out[attr.key] = CurrentValue(value=value, raw=raw, source=source, errors=errors)
    return out


def _sentences(sources: list[TextSource]) -> list[tuple[str, str]]:
    out = []
    for src in sources:
        for sentence in SENTENCE_RE.split(src.text):
            sentence = sentence.strip()
            if sentence and not INSTRUCTION_RE.search(sentence):
                out.append((src.name, sentence[:400]))
    return out


def _measure_re(kind: str) -> re.Pattern[str]:
    units = sorted(UNIT_ALIASES[kind], key=len, reverse=True)
    alternation = "|".join(re.escape(u) for u in units if u != '"')
    suffix = r'|"' if kind == "dimension" else ""
    return re.compile(
        rf"(?<![\w.]){NUMBER_RE}\s*-?\s*({alternation}{suffix})(?![a-z])", re.IGNORECASE
    )


MEASURE_RES = {kind: _measure_re(kind) for kind in MEASUREMENT_TYPES}


def _alias_hit(attr: AttributeDefinition, sentence: str) -> bool:
    terms = [attr.label.lower(), attr.key.replace("_", " "), *[a.lower() for a in attr.aliases]]
    lowered = sentence.lower()
    return any(t and t in lowered for t in terms)


def extract_from_text(attr: AttributeDefinition, sources: list[TextSource]) -> Extraction | None:
    sentences = _sentences(sources)
    if attr.type in MEASUREMENT_TYPES:
        return _extract_measurement(attr, sentences)
    if attr.type == "number" and attr.pattern:
        pattern = re.compile(attr.pattern, re.IGNORECASE)
        for source, sentence in sentences:
            match = pattern.search(sentence)
            if match:
                try:
                    value = parse_value(match.group(1), attr)
                except InvalidValue:
                    continue
                return Extraction(value, sentence, source, 0.85)
        return None
    if attr.type == "boolean":
        for source, sentence in sentences:
            if attr.aliases and _alias_hit_terms(attr.aliases, sentence):
                if NEGATION_RE.search(sentence):
                    continue  # "no laptop sleeve" is not evidence either way we trust
                return Extraction(True, sentence, source, 0.8)
        return None
    if attr.allowed_values:
        return _extract_vocabulary(attr, sentences)
    if attr.aliases:
        for source, sentence in sentences:
            if source == "description" and _alias_hit_terms(attr.aliases, sentence):
                return Extraction(sentence[:300], sentence, source, 0.6)
    return None


def _alias_hit_terms(terms: list[str], sentence: str) -> bool:
    lowered = sentence.lower()
    return any(re.search(rf"\b{re.escape(t.lower())}\b", lowered) for t in terms)


def _extract_measurement(
    attr: AttributeDefinition, sentences: list[tuple[str, str]]
) -> Extraction | None:
    regex = MEASURE_RES[attr.type]
    best: Extraction | None = None
    for source, sentence in sentences:
        for match in regex.finditer(sentence):
            try:
                value = parse_measurement(f"{match.group(1)} {match.group(2)}", attr.type)
            except InvalidValue:
                continue
            if _alias_hit(attr, sentence):
                return Extraction(value, sentence, source, 0.9)
            # A unit alone (e.g. "28L" in a title) is weaker evidence: other numbers with the
            # same unit (a load limit, a bottle size) could match.
            confidence = 0.7 if source == "title" else 0.55
            if best is None or confidence > best.confidence:
                best = Extraction(value, sentence, source, confidence)
    return best


def _extract_vocabulary(
    attr: AttributeDefinition, sentences: list[tuple[str, str]]
) -> Extraction | None:
    found: list[str] = []
    evidence: list[tuple[str, str]] = []
    vocab = sorted(attr.allowed_values or [], key=len, reverse=True)
    for source, sentence in sentences:
        lowered = sentence.lower()
        for term in vocab:
            if re.search(rf"(?<![\w-]){re.escape(term.lower())}(?![\w-])", lowered):
                # Skip a shorter term already covered by a longer one ("nylon" in "ripstop nylon").
                if any(term.lower() in f.lower() for f in found):
                    continue
                found.append(term)
                if (source, sentence) not in evidence:
                    evidence.append((source, sentence))
    if not found:
        return None
    if attr.type == "list":
        value: Any = found
    else:
        if len(found) > 1 and attr.key != "material":
            return None  # conflicting single values ("down" and "synthetic"): leave unknown
        value = " / ".join(found) if attr.key == "material" else found[0]
    source = evidence[0][0]
    quote = " … ".join(s for _, s in evidence[:3])
    confidence = 0.75 if source in ("description", "title") else 0.6
    return Extraction(value, quote, source, confidence)
