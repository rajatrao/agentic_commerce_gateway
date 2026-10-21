"""Meta-mapping: derive structured product attributes from merchant tags, metafields and fields.

Merchants often encode facts as tags ("material:organic-cotton", "GTIN-0123456789012") or in
custom metafields. Mapping rules turn those into schema.org / protocol properties, so the
auditor credits them and the llm.txt feed and protocol adapters can expose them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SOURCE_TYPES = ("tag", "metafield", "product_type", "vendor")


@dataclass(frozen=True)
class MappingRule:
    source_type: str
    match_pattern: str
    target_property: str
    source_key: str = ""
    transform: dict[str, Any] = field(default_factory=dict)
    priority: int = 100

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.match_pattern, re.IGNORECASE)


def _candidates(rule: MappingRule, product: dict[str, Any]) -> list[str]:
    if rule.source_type == "tag":
        return [str(t) for t in product.get("tags") or []]
    if rule.source_type == "product_type":
        return [str(product.get("productType") or "")]
    if rule.source_type == "vendor":
        return [str(product.get("vendor") or "")]
    if rule.source_type == "metafield":
        namespace, _, key = rule.source_key.partition(".")
        nodes = (product.get("metafields") or {}).get("nodes") or []
        return [
            str(m.get("value") or "")
            for m in nodes
            if m.get("namespace") == namespace and m.get("key") == key
        ]
    return []


def _apply_transform(value: str, transform: dict[str, Any]) -> str:
    if "value" in transform:  # constant output, e.g. tag "organic" -> material "Organic cotton"
        return str(transform["value"])
    if transform.get("replace_dashes", True):
        value = value.replace("-", " ").replace("_", " ")
    case = transform.get("case")
    if case == "title":
        value = value.title()
    elif case == "upper":
        value = value.upper()
    elif case == "lower":
        value = value.lower()
    return value.strip()


def apply_rules(product: dict[str, Any], rules: list[MappingRule]) -> dict[str, str]:
    """Return {target_property: value}. Lower `priority` wins when rules collide."""
    result: dict[str, str] = {}
    for rule in sorted(rules, key=lambda r: r.priority):
        if rule.target_property in result:
            continue
        pattern = rule.compiled()
        for candidate in _candidates(rule, product):
            match = pattern.fullmatch(candidate.strip())
            if not match:
                continue
            raw = match.group(1) if match.groups() else candidate
            transform = rule.transform
            if rule.target_property == "gtin":
                transform = {**transform, "replace_dashes": False}
            value = _apply_transform(raw, transform)
            if value:
                result[rule.target_property] = value
                break
    return result


def preview_rule(
    rule: MappingRule,
    products: list[dict[str, Any]],
    others: list[MappingRule],
    *,
    max_matches: int = 25,
    max_unmatched: int = 20,
) -> dict[str, Any]:
    """What `rule` alone extracts from `products`. `others` are the merchant's active rules:
    a match is flagged when one of them already sets the same property with a lower
    priority number, because that rule would win."""
    stronger = [
        r
        for r in others
        if r.target_property == rule.target_property and r.priority < rule.priority
    ]
    pattern = rule.compiled()
    matches: list[dict[str, Any]] = []
    matched = 0
    unmatched: list[str] = []
    seen: set[str] = set()
    for product in products:
        source = None
        for candidate in _candidates(rule, product):
            text = candidate.strip()
            if not text:
                continue
            if pattern.fullmatch(text):
                source = source or text
            elif text.lower() not in seen and len(unmatched) < max_unmatched:
                seen.add(text.lower())
                unmatched.append(text)
        # apply_rules takes the first matching value, the same one `source` holds.
        value = apply_rules(product, [rule]).get(rule.target_property) if source else None
        if not value:
            continue
        matched += 1
        if len(matches) < max_matches:
            matches.append(
                {
                    "product_gid": str(product.get("id") or ""),
                    "title": str(product.get("title") or ""),
                    "source_value": source,
                    "value": value,
                    "overridden_by_rule": bool(
                        stronger and rule.target_property in apply_rules(product, stronger)
                    ),
                }
            )
    return {
        "products_scanned": len(products),
        "matched": matched,
        "matches": matches,
        "unmatched_values": unmatched,
    }
