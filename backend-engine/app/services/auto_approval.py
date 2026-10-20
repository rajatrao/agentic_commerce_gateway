"""Opt-in automatic approval of high-confidence, rule-confirmed suggestions.

Each category schema can enable it (`CategorySchema.auto_approve`, off by default). The rules
are deliberately narrow: only values that fill an empty attribute, passed validation without
warnings, were read by the deterministic extractor from the product's own text, and meet the
schema's confidence threshold. Auto-approval stops short of publishing; the merchant still
previews and confirms the Shopify write, and the auto-approval report lists every value
approved this way with its evidence so it can be sent back to review.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from app.services.catalog_schema import AutoApproval


class Candidate(Protocol):
    attribute_key: str
    current_value: Any
    proposed_value: Any
    validation_status: str
    rule_confirmed: bool
    confidence: Decimal


def unmet_conditions(rule: AutoApproval, s: Candidate) -> list[str]:
    """Why `s` would not be auto-approved under `rule` (empty: it qualifies). The schema's
    `enabled` flag is not checked here, so the report can preview a rule before it is on."""
    unmet: list[str] = []
    if rule.attributes is not None and s.attribute_key not in rule.attributes:
        unmet.append("attribute not included")
    if s.proposed_value is None:
        unmet.append("no value found")
    if s.current_value is not None:
        unmet.append("would replace an existing value")
    if s.validation_status != "valid":
        unmet.append("validation warnings")
    if not s.rule_confirmed:
        unmet.append("not confirmed by rule-based extraction")
    if Decimal(str(s.confidence)) < Decimal(str(rule.min_confidence)):
        unmet.append(f"confidence below {rule.min_confidence:.2f}")
    return unmet


def qualifies(rule: AutoApproval | None, s: Candidate) -> bool:
    return rule is not None and rule.enabled and not unmet_conditions(rule, s)


def change_details(rule: AutoApproval, s: Any) -> dict[str, Any]:
    """What the catalog change log records for an auto-approval, for the report."""
    return {
        "rule": {
            "min_confidence": rule.min_confidence,
            "attributes": rule.attributes,
        },
        "confidence": float(s.confidence),
        "method": s.method,
        "evidence": s.evidence,
        "evidence_source": s.evidence_source,
    }
