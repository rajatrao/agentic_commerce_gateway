"""Merchant review decisions and Shopify write-back for attribute suggestions.

Nothing reaches Shopify without an explicit merchant approval: `review` moves suggestions to
approved (optionally with an edited value) or rejected, and only approved suggestions can be
previewed and published. Every decision and every write (with the value it replaced) is
recorded in `catalog_changes`, which is what makes a write revertible.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import cast, literal, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    AttributeSuggestion,
    CatalogChange,
    CatalogProduct,
    Merchant,
    SuggestionStatus,
)
from app.models.base import utcnow
from app.repositories.catalog import CatalogRepository
from app.repositories.catalog_config import CatalogConfig
from app.repositories.enrichment import EnrichmentRepository
from app.services.catalog_schema import (
    AttributeDefinition,
    InvalidValue,
    parse_value,
    validate_value,
)
from app.services.metafield_writer import MetafieldWriter, PlanItem, WriteRequest


class ReviewError(Exception):
    pass


@dataclass
class ReviewAction:
    id: uuid.UUID
    action: Literal["approve", "edit", "reject"]
    value: Any = None


def attribute_for(config: CatalogConfig, s: AttributeSuggestion) -> AttributeDefinition | None:
    schema = config.schemas.get(s.schema_key)
    attr = schema.attribute(s.attribute_key) if schema else None
    if attr is None:
        for other in config.schemas.values():
            attr = other.attribute(s.attribute_key)
            if attr:
                break
    return attr


def _change(
    s: AttributeSuggestion, action: str, before: Any, after: Any, **details: Any
) -> CatalogChange:
    return CatalogChange(
        merchant_id=s.merchant_id,
        product_gid=s.product_gid,
        product_title=s.product_title,
        suggestion_id=s.id,
        attribute_key=s.attribute_key,
        action=action,
        before=before,
        after=after,
        details=details,
    )


async def review(
    session: AsyncSession,
    merchant: Merchant,
    config: CatalogConfig,
    actions: list[ReviewAction],
) -> list[dict[str, Any]]:
    """Apply approve / edit-and-approve / reject decisions. Each item succeeds or fails on its
    own; failures are reported, not raised."""
    rows = {
        s.id: s
        for s in await EnrichmentRepository(session).lock_suggestions(
            merchant.id, [a.id for a in actions]
        )
    }
    results: list[dict[str, Any]] = []
    now = utcnow()
    reviewable = (SuggestionStatus.PENDING, SuggestionStatus.APPROVED, SuggestionStatus.FAILED)
    for a in actions:
        s = rows.get(a.id)
        if s is None:
            results.append({"id": str(a.id), "ok": False, "error": "suggestion not found"})
            continue
        if s.status not in reviewable:
            results.append(
                {"id": str(a.id), "ok": False, "error": f"suggestion is {s.status.value}"}
            )
            continue
        attr = attribute_for(config, s)
        if a.action == "reject":
            session.add(_change(s, "reject", s.proposed_value, None))
            s.status, s.reviewed_at = SuggestionStatus.REJECTED, now
        elif a.action == "approve":
            if s.proposed_value is None:
                results.append(
                    {
                        "id": str(a.id),
                        "ok": False,
                        "error": "the value is unknown; enter a value to approve it",
                    }
                )
                continue
            if s.validation_status == "invalid":
                results.append(
                    {"id": str(a.id), "ok": False, "error": "the value failed validation"}
                )
                continue
            session.add(_change(s, "approve", s.current_value, s.proposed_value))
            s.status, s.approved_value, s.reviewed_at = (
                SuggestionStatus.APPROVED,
                s.proposed_value,
                now,
            )
        else:
            if attr is None:
                results.append(
                    {"id": str(a.id), "ok": False, "error": "attribute is no longer in a schema"}
                )
                continue
            try:
                value = parse_value(a.value, attr)
            except InvalidValue as exc:
                results.append({"id": str(a.id), "ok": False, "error": str(exc)})
                continue
            warnings = validate_value(value, attr)
            session.add(_change(s, "edit_approve", s.proposed_value, value, warnings=warnings))
            s.status, s.approved_value, s.reviewed_at = SuggestionStatus.APPROVED, value, now
            s.method = f"{s.method}+merchant_edit" if "merchant_edit" not in s.method else s.method
            s.validation_status = "warning" if warnings else "valid"
            s.validation_errors = warnings
        s.updated_at = now
        s.last_error = None
        session.add(s)
        results.append({"id": str(a.id), "ok": True, "status": s.status.value})
    await session.commit()
    return results


async def _approved(
    session: AsyncSession, merchant: Merchant, ids: list[uuid.UUID] | None, limit: int, lock: bool
) -> list[AttributeSuggestion]:
    stmt = select(AttributeSuggestion).where(
        AttributeSuggestion.merchant_id == merchant.id,
        AttributeSuggestion.status == SuggestionStatus.APPROVED,
    )
    if ids is not None:
        stmt = stmt.where(col(AttributeSuggestion.id).in_(ids))
    stmt = stmt.order_by(col(AttributeSuggestion.product_gid)).limit(limit)
    if lock:
        stmt = stmt.with_for_update(skip_locked=True)
    return list((await session.exec(stmt)).all())


def _requests(config: CatalogConfig, rows: list[AttributeSuggestion]) -> list[WriteRequest]:
    out = []
    for s in rows:
        attr = attribute_for(config, s)
        mapping = config.mappings.get(s.attribute_key)
        if attr is None or mapping is None:
            continue
        out.append(
            WriteRequest(
                suggestion_id=str(s.id),
                product_gid=s.product_gid,
                product_title=s.product_title,
                attribute=attr,
                value=s.approved_value,
                namespace=mapping.namespace,
                key=mapping.key,
                type=mapping.type,
                enabled=mapping.enabled,
            )
        )
    return out


def _unmapped(config: CatalogConfig, rows: list[AttributeSuggestion]) -> list[dict[str, Any]]:
    return [
        {
            "suggestion_id": str(s.id),
            "product_gid": s.product_gid,
            "product_title": s.product_title,
            "attribute_key": s.attribute_key,
            "action": "blocked",
            "problems": ["no metafield mapping for this attribute"],
        }
        for s in rows
        if attribute_for(config, s) is None or s.attribute_key not in config.mappings
    ]


async def preview(
    session: AsyncSession,
    merchant: Merchant,
    config: CatalogConfig,
    writer: MetafieldWriter,
    ids: list[uuid.UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    rows = await _approved(session, merchant, ids, limit, lock=False)
    items = await writer.plan(_requests(config, rows))
    return [i.as_json() for i in items] + _unmapped(config, rows)


async def publish(
    session: AsyncSession,
    merchant: Merchant,
    config: CatalogConfig,
    writer: MetafieldWriter,
    ids: list[uuid.UUID] | None,
    limit: int,
) -> dict[str, Any]:
    """Write approved values to Shopify. Rows are locked for the duration, so a concurrent
    publish of the same suggestions skips them instead of writing twice."""
    rows = await _approved(session, merchant, ids, limit, lock=True)
    by_id = {str(s.id): s for s in rows}
    outcomes = await writer.publish(await writer.plan(_requests(config, rows)))
    now = utcnow()
    published = failed = unchanged = 0
    snapshot: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        item: PlanItem = outcome.item
        s = by_id[item.suggestion_id]
        metafield = f"{item.namespace}.{item.key}"
        before = (
            {"value": item.current_value, "type": item.current_type}
            if item.current_value is not None
            else None
        )
        after = {"value": item.value, "type": item.type}
        if outcome.ok:
            s.status, s.published_at, s.last_error = SuggestionStatus.PUBLISHED, now, None
            if item.action == "unchanged":
                unchanged += 1
            else:
                published += 1
                session.add(_change(s, "publish", before, after, metafield=metafield))
            snapshot.setdefault(s.product_gid, {})[s.attribute_key] = {
                "value": s.approved_value,
                "raw": item.value,
                "source": f"metafield:{metafield}",
                "errors": [],
            }
        else:
            failed += 1
            s.status, s.last_error = (
                SuggestionStatus.FAILED,
                (outcome.error or "write failed")[:1000],
            )
            session.add(
                _change(s, "publish_failed", before, after, metafield=metafield, error=s.last_error)
            )
        s.updated_at = now
        session.add(s)
    for s in rows:
        if s.attribute_key not in config.mappings or attribute_for(config, s) is None:
            failed += 1
            s.status, s.last_error = SuggestionStatus.FAILED, "no metafield mapping"
            session.add(s)
    for gid, attrs in snapshot.items():
        await _patch_snapshot(session, merchant.id, gid, attrs)
    await session.commit()
    return {"published": published, "unchanged": unchanged, "failed": failed}


async def _patch_snapshot(
    session: AsyncSession, merchant_id: uuid.UUID, gid: str, attrs: dict[str, Any]
) -> None:
    patch = cast(literal(json.dumps(attrs)), JSONB)
    await session.exec(
        update(CatalogProduct)
        .where(
            col(CatalogProduct.merchant_id) == merchant_id,
            col(CatalogProduct.product_gid) == gid,
        )
        .values(attributes=col(CatalogProduct.attributes).op("||")(patch))
    )


async def revert(
    session: AsyncSession, merchant: Merchant, writer: MetafieldWriter, change_id: int
) -> CatalogChange:
    change = await CatalogRepository(session).change(merchant.id, change_id)
    if change is None:
        raise ReviewError("change not found")
    if change.action != "publish":
        raise ReviewError("only Shopify writes can be reverted")
    if change.reverted_at is not None:
        raise ReviewError("this change was already reverted")
    namespace, key = str(change.details.get("metafield", ".")).split(".", 1)
    error = await writer.revert(change.product_gid, namespace, key, change.before)
    if error:
        raise ReviewError(f"Shopify rejected the revert: {error}")
    now = utcnow()
    change.reverted_at = now
    session.add(change)
    revert_row = CatalogChange(
        merchant_id=merchant.id,
        product_gid=change.product_gid,
        product_title=change.product_title,
        suggestion_id=change.suggestion_id,
        attribute_key=change.attribute_key,
        action="revert",
        before=change.after,
        after=change.before,
        details={"metafield": f"{namespace}.{key}", "reverts": change.id},
    )
    session.add(revert_row)
    if change.suggestion_id:
        await session.exec(
            update(AttributeSuggestion)
            .where(col(AttributeSuggestion.id) == change.suggestion_id)
            .values(status=SuggestionStatus.REVERTED, updated_at=now)
        )
    if change.attribute_key:
        product = await CatalogRepository(session).product(merchant.id, change.product_gid)
        if product is not None:
            attrs = dict(product.attributes)
            if change.before is None:
                attrs.pop(change.attribute_key, None)
            else:
                attrs[change.attribute_key] = {
                    "value": None,
                    "raw": change.before.get("value"),
                    "source": f"metafield:{namespace}.{key}",
                    "errors": ["restored previous value; re-run the audit to re-validate"],
                }
            product.attributes = attrs
            session.add(product)
    await session.commit()
    await session.refresh(revert_row)
    return revert_row
