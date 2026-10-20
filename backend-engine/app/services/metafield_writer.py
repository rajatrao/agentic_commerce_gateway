"""Write approved attribute values to Shopify product metafields.

Flow: `plan()` reads each product's current metafields and the shop's metafield definitions
and returns one item per suggestion (create / update / unchanged, or a blocking problem);
`publish()` writes the writable items with `metafieldsSet`, passing each existing value's
`compareDigest` so a concurrent edit in Shopify admin is never overwritten; `revert()` puts the
previous value back (or deletes a metafield the app created).

Values are typed: each attribute type can only be written to compatible metafield types, and a
mapping whose namespace/key already has a definition (or values) of a different type is a
conflict that blocks the write rather than creating a duplicate or incompatible field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.catalog_schema import (
    MEASUREMENT_TYPES,
    AttributeDefinition,
    InvalidValue,
    convert,
    parse_value,
)
from app.services.shopify_client import ShopifyAdminClient

COMPATIBLE_TYPES: dict[str, set[str]] = {
    "text": {"single_line_text_field", "multi_line_text_field"},
    "list": {"list.single_line_text_field", "single_line_text_field"},
    "number": {"number_decimal", "number_integer"},
    "boolean": {"boolean"},
    "weight": {"weight", "number_decimal"},
    "volume": {"volume", "number_decimal"},
    "dimension": {"dimension", "number_decimal"},
}
SET_BATCH = 25  # metafieldsSet accepts at most 25 metafields per call

DEFINITIONS_QUERY = """
query ProductMetafieldDefinitions($cursor: String) {
  metafieldDefinitions(ownerType: PRODUCT, first: 250, after: $cursor) {
    nodes { id name namespace key type { name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PRODUCT_METAFIELDS_QUERY = """
query ProductMetafieldsForWrite($id: ID!) {
  product(id: $id) {
    id
    title
    metafields(first: 250) { nodes { namespace key type value compareDigest } }
  }
}
"""

SET_MUTATION = """
mutation SetProductMetafields($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { namespace key type value compareDigest }
    userErrors { field message code elementIndex }
  }
}
"""

DELETE_MUTATION = """
mutation DeleteProductMetafields($metafields: [MetafieldIdentifierInput!]!) {
  metafieldsDelete(metafields: $metafields) {
    deletedMetafields { namespace key ownerId }
    userErrors { field message }
  }
}
"""

CREATE_DEFINITION_MUTATION = """
mutation CreateProductMetafieldDefinition($definition: MetafieldDefinitionInput!) {
  metafieldDefinitionCreate(definition: $definition) {
    createdDefinition { id namespace key type { name } }
    userErrors { field message code }
  }
}
"""


def _plain(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return text


def serialize(value: Any, attr: AttributeDefinition, metafield_type: str) -> str:
    """Canonical attribute value -> the string Shopify expects for `metafield_type`."""
    if metafield_type not in COMPATIBLE_TYPES[attr.type]:
        raise InvalidValue(f"a {attr.type} attribute cannot be stored as {metafield_type}")
    value = parse_value(value, attr)
    if attr.type in MEASUREMENT_TYPES:
        if metafield_type == "number_decimal":
            assert attr.unit is not None
            return _plain(round(convert(value, attr.type, attr.unit), 3))
        return json.dumps({"value": value["value"], "unit": value["unit"]})
    if attr.type == "number":
        number = Decimal(str(value))
        if metafield_type == "number_integer":
            if number != number.to_integral_value():
                raise InvalidValue(f"{number} is not a whole number")
            return str(int(number))
        return _plain(number)
    if attr.type == "boolean":
        return "true" if value else "false"
    if attr.type == "list":
        if metafield_type == "single_line_text_field":
            return ", ".join(value)
        return json.dumps(value)
    if metafield_type == "single_line_text_field" and "\n" in value:
        raise InvalidValue("single-line text cannot contain line breaks")
    return str(value)


@dataclass
class PlanItem:
    suggestion_id: str
    product_gid: str
    product_title: str
    attribute_key: str
    namespace: str
    key: str
    type: str
    value: str | None
    current_value: str | None = None
    current_type: str | None = None
    compare_digest: str | None = None
    action: str = "create"  # create | update | unchanged | blocked
    problems: list[str] = field(default_factory=list)
    needs_definition: bool = False

    @property
    def writable(self) -> bool:
        return self.action in ("create", "update") and not self.problems

    def as_json(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "product_gid": self.product_gid,
            "product_title": self.product_title,
            "attribute_key": self.attribute_key,
            "metafield": f"{self.namespace}.{self.key}",
            "type": self.type,
            "value": self.value,
            "current_value": self.current_value,
            "current_type": self.current_type,
            "action": self.action,
            "problems": self.problems,
            "creates_definition": self.needs_definition,
        }


@dataclass
class WriteRequest:
    suggestion_id: str
    product_gid: str
    product_title: str
    attribute: AttributeDefinition
    value: Any
    namespace: str
    key: str
    type: str
    enabled: bool = True


@dataclass
class WriteOutcome:
    item: PlanItem
    ok: bool
    error: str | None = None
    written: dict[str, Any] | None = None


class MetafieldWriter:
    def __init__(self, client: ShopifyAdminClient) -> None:
        self.client = client
        self._definitions: dict[tuple[str, str], str] | None = None

    async def definitions(self) -> dict[tuple[str, str], str]:
        if self._definitions is None:
            self._definitions = {}
            async for node in self.client.paginate(DEFINITIONS_QUERY, ["metafieldDefinitions"]):
                self._definitions[(node["namespace"], node["key"])] = node["type"]["name"]
        return self._definitions

    async def _product(self, gid: str) -> dict[str, Any] | None:
        data = await self.client.query(PRODUCT_METAFIELDS_QUERY, {"id": gid})
        return data.get("product")

    async def plan(self, requests: list[WriteRequest]) -> list[PlanItem]:
        definitions = await self.definitions()
        products: dict[str, dict[str, Any] | None] = {}
        items: list[PlanItem] = []
        for req in requests:
            item = PlanItem(
                suggestion_id=req.suggestion_id,
                product_gid=req.product_gid,
                product_title=req.product_title,
                attribute_key=req.attribute.key,
                namespace=req.namespace,
                key=req.key,
                type=req.type,
                value=None,
            )
            items.append(item)
            if not req.enabled:
                item.action, item.problems = "blocked", ["the mapping for this attribute is off"]
                continue
            try:
                item.value = serialize(req.value, req.attribute, req.type)
            except InvalidValue as exc:
                item.action, item.problems = "blocked", [str(exc)]
                continue
            defined = definitions.get((req.namespace, req.key))
            if defined is not None and defined != req.type:
                item.action = "blocked"
                item.problems.append(
                    f"{req.namespace}.{req.key} is already defined as {defined}; change the "
                    "mapping to that type or to another key"
                )
                continue
            item.needs_definition = defined is None
            if req.product_gid not in products:
                products[req.product_gid] = await self._product(req.product_gid)
            product = products[req.product_gid]
            if product is None:
                item.action, item.problems = "blocked", ["the product no longer exists in Shopify"]
                continue
            item.product_title = product.get("title") or item.product_title
            existing = next(
                (
                    m
                    for m in (product.get("metafields") or {}).get("nodes") or []
                    if m["namespace"] == req.namespace and m["key"] == req.key
                ),
                None,
            )
            if existing is not None:
                item.current_value, item.current_type = existing["value"], existing["type"]
                item.compare_digest = existing.get("compareDigest")
                if existing["type"] != req.type:
                    item.action = "blocked"
                    item.problems.append(
                        f"the product already has {req.namespace}.{req.key} as "
                        f"{existing['type']}; writing {req.type} would conflict"
                    )
                    continue
                item.action = "unchanged" if existing["value"] == item.value else "update"
        return items

    async def ensure_definitions(self, items: list[PlanItem]) -> dict[tuple[str, str], str]:
        """Create missing definitions so values are typed and visible in Shopify admin.
        Returns errors by (namespace, key)."""
        errors: dict[tuple[str, str], str] = {}
        definitions = await self.definitions()
        for ns, key, type_, attr_key in sorted(
            {(i.namespace, i.key, i.type, i.attribute_key) for i in items if i.needs_definition}
        ):
            if (ns, key) in definitions:
                continue
            data = await self.client.query(
                CREATE_DEFINITION_MUTATION,
                {
                    "definition": {
                        "name": attr_key.replace("_", " ").capitalize(),
                        "namespace": ns,
                        "key": key,
                        "type": type_,
                        "ownerType": "PRODUCT",
                        "description": "Managed by Agentic Commerce Gateway catalog enrichment.",
                    }
                },
            )
            result = data["metafieldDefinitionCreate"]
            user_errors = result.get("userErrors") or []
            # TAKEN: someone created it meanwhile; the write itself will surface a type clash.
            if user_errors and not all(e.get("code") == "TAKEN" for e in user_errors):
                errors[(ns, key)] = "; ".join(e["message"] for e in user_errors)
            else:
                definitions[(ns, key)] = type_
        return errors

    async def publish(self, items: list[PlanItem]) -> list[WriteOutcome]:
        writable = [i for i in items if i.writable]
        outcomes: list[WriteOutcome] = [
            WriteOutcome(i, ok=i.action == "unchanged", error="; ".join(i.problems) or None)
            for i in items
            if not i.writable
        ]
        definition_errors = await self.ensure_definitions(writable)
        ready: list[PlanItem] = []
        for item in writable:
            err = definition_errors.get((item.namespace, item.key))
            if err:
                outcomes.append(
                    WriteOutcome(item, ok=False, error=f"could not create definition: {err}")
                )
            else:
                ready.append(item)
        for start in range(0, len(ready), SET_BATCH):
            batch = ready[start : start + SET_BATCH]
            inputs = []
            for item in batch:
                entry: dict[str, Any] = {
                    "ownerId": item.product_gid,
                    "namespace": item.namespace,
                    "key": item.key,
                    "type": item.type,
                    "value": item.value,
                }
                if item.compare_digest:
                    entry["compareDigest"] = item.compare_digest
                inputs.append(entry)
            data = await self.client.query(SET_MUTATION, {"metafields": inputs})
            result = data["metafieldsSet"]
            failed: dict[int, str] = {}
            for err in result.get("userErrors") or []:
                index = err.get("elementIndex")
                if index is None:
                    # A batch-level error: nothing in this batch was written.
                    failed |= {i: err["message"] for i in range(len(batch))}
                else:
                    failed[int(index)] = err["message"]
            written = {(m["namespace"], m["key"]): m for m in result.get("metafields") or []}
            for i, item in enumerate(batch):
                if i in failed:
                    outcomes.append(WriteOutcome(item, ok=False, error=failed[i]))
                else:
                    outcomes.append(
                        WriteOutcome(item, ok=True, written=written.get((item.namespace, item.key)))
                    )
        return outcomes

    async def revert(
        self, product_gid: str, namespace: str, key: str, before: dict[str, Any] | None
    ) -> str | None:
        """Restore the value recorded before a write. Returns an error message or None."""
        if before is None or before.get("value") is None:
            data = await self.client.query(
                DELETE_MUTATION,
                {"metafields": [{"ownerId": product_gid, "namespace": namespace, "key": key}]},
            )
            errors = data["metafieldsDelete"].get("userErrors") or []
        else:
            data = await self.client.query(
                SET_MUTATION,
                {
                    "metafields": [
                        {
                            "ownerId": product_gid,
                            "namespace": namespace,
                            "key": key,
                            "type": before["type"],
                            "value": before["value"],
                        }
                    ]
                },
            )
            errors = data["metafieldsSet"].get("userErrors") or []
        return "; ".join(e["message"] for e in errors) or None
