"""Write approved values to Shopify's standard category metafields as well.

Shopify's standard product taxonomy gives each category a set of attributes (Color, Material,
Activity, ...). When a product has a category, its values for those attributes live in
category metafields in the reserved `shopify` namespace, typed `list.metaobject_reference`:
each value is a metaobject entry (type `shopify--<key>`) that points at a taxonomy value. This
is the structured data Shopify's own search, filters and agent catalog read.

An attribute opts in with `taxonomy_attribute` (the taxonomy attribute's name). For each
approved value the writer:

1. reads the product's category and its choice-list attributes, and finds the one with that
   name (an exact match, or a single attribute whose name ends with it, so "Material" also
   finds "Bag/Case material");
2. matches the value to that attribute's taxonomy values by name (whole words, case-blind);
   values that match nothing are skipped and listed, never guessed;
3. finds the store's category metafield definition with the attribute's name, and the
   metaobject entries for the matched values (creating missing entries on publish);
4. adds the entries to the product's existing list (nothing the merchant chose is removed)
   and writes it with `metafieldsSet` and `compareDigest`.

Anything missing (no category, no matching attribute or definition) makes the item a skip with
the reason; the custom metafield write is never affected.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.catalog_schema import AttributeDefinition
from app.services.metafield_writer import SET_MUTATION
from app.services.shopify_client import ShopifyAdminClient, ShopifyAuthError, ShopifyError

NAMESPACE = "shopify"
LIST_TYPE = "list.metaobject_reference"

CATEGORY_QUERY = """
query ProductCategoryAttributes($id: ID!) {
  product(id: $id) {
    id
    title
    category {
      id
      fullName
      attributes(first: 50) {
        nodes {
          __typename
          ... on TaxonomyChoiceListAttribute {
            id
            name
            values(first: 250) { nodes { id name } }
          }
        }
      }
    }
    metafields(first: 100, namespace: "shopify") {
      nodes { namespace key type value compareDigest }
    }
  }
}
"""

CATEGORY_DEFINITIONS_QUERY = """
query CategoryMetafieldDefinitions($cursor: String) {
  metafieldDefinitions(ownerType: PRODUCT, namespace: "shopify", first: 250, after: $cursor) {
    nodes { name namespace key type { name } validations { name value } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

METAOBJECT_DEFINITION_QUERY = """
query TaxonomyMetaobjectDefinition($id: ID!) {
  metaobjectDefinition(id: $id) { type }
}
"""

ENTRIES_QUERY = """
query TaxonomyEntries($type: String!, $cursor: String) {
  metaobjects(type: $type, first: 250, after: $cursor) {
    nodes { id fields { key value } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

CREATE_ENTRY_MUTATION = """
mutation CreateTaxonomyEntry($metaobject: MetaobjectCreateInput!) {
  metaobjectCreate(metaobject: $metaobject) {
    metaobject { id }
    userErrors { field message code }
  }
}
"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def match_values(raw: Any, choices: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    """Taxonomy values named in `raw` (a string or list), and the items that matched none."""
    items = raw if isinstance(raw, list) else [raw]
    matched: dict[str, dict[str, str]] = {}
    unmatched: list[str] = []
    for item in (str(i) for i in items if i is not None and str(i).strip()):
        text = _norm(item)
        exact = [c for c in choices if _norm(c["name"]) == text]
        hits = exact or [
            c for c in choices if re.search(rf"\b{re.escape(_norm(c['name']))}\b", text)
        ]
        if not hits:
            unmatched.append(item)
        for c in hits:
            matched[c["id"]] = c
    return list(matched.values()), unmatched


def find_attribute(category: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    nodes = [
        n
        for n in ((category or {}).get("attributes") or {}).get("nodes") or []
        if n.get("__typename") == "TaxonomyChoiceListAttribute"
    ]
    target = _norm(name)
    exact = [n for n in nodes if _norm(n.get("name") or "") == target]
    if exact:
        return exact[0]
    suffix = [n for n in nodes if _norm(n.get("name") or "").endswith(" " + target)]
    suffix += [n for n in nodes if _norm(n.get("name") or "").endswith("/" + target)]
    unique = {n["id"]: n for n in suffix}
    return next(iter(unique.values())) if len(unique) == 1 else None


@dataclass
class TaxonomyRequest:
    suggestion_id: str
    product_gid: str
    product_title: str
    attribute: AttributeDefinition
    value: Any


@dataclass
class TaxonomyItem:
    suggestion_ids: list[str]
    product_gid: str
    product_title: str
    attribute_key: str
    taxonomy_attribute: str
    key: str | None = None
    metaobject_type: str | None = None
    values: list[dict[str, str]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    current_value: str | None = None
    compare_digest: str | None = None
    entry_ids: dict[str, str] = field(default_factory=dict)  # taxonomy value id -> entry id
    action: str = "skipped"  # add | unchanged | skipped
    reason: str | None = None

    @property
    def metafield(self) -> str:
        return f"{NAMESPACE}.{self.key}" if self.key else f"{NAMESPACE}.?"

    def current_ids(self) -> list[str]:
        try:
            parsed = json.loads(self.current_value or "[]")
        except json.JSONDecodeError:
            return []
        return [str(i) for i in parsed] if isinstance(parsed, list) else []

    def as_json(self) -> dict[str, Any]:
        return {
            "suggestion_ids": self.suggestion_ids,
            "product_gid": self.product_gid,
            "product_title": self.product_title,
            "attribute_key": self.attribute_key,
            "taxonomy_attribute": self.taxonomy_attribute,
            "metafield": self.metafield,
            "values": [v["name"] for v in self.values],
            "unmatched": self.unmatched,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass
class TaxonomyOutcome:
    item: TaxonomyItem
    ok: bool
    before: str | None = None
    after: str | None = None
    error: str | None = None


class TaxonomyWriter:
    def __init__(self, client: ShopifyAdminClient) -> None:
        self.client = client
        self._definitions: dict[str, dict[str, Any]] | None = None
        self._types: dict[str, str] = {}
        self._entries: dict[str, dict[str, str]] = {}

    async def definitions(self) -> dict[str, dict[str, Any]]:
        """The store's `shopify` namespace product definitions, by lower-cased name."""
        if self._definitions is None:
            found: dict[str, dict[str, Any]] = {}
            async for node in self.client.paginate(
                CATEGORY_DEFINITIONS_QUERY, ["metafieldDefinitions"]
            ):
                if node["type"]["name"] == LIST_TYPE:
                    found[_norm(node["name"])] = node
            self._definitions = found
        return self._definitions

    async def _metaobject_type(self, definition: dict[str, Any]) -> str:
        key = definition["key"]
        if key not in self._types:
            ref = next(
                (
                    v["value"]
                    for v in definition.get("validations") or []
                    if v.get("name") == "metaobject_definition_id"
                ),
                None,
            )
            type_ = f"shopify--{key}"
            if ref:
                data = await self.client.query(METAOBJECT_DEFINITION_QUERY, {"id": ref})
                type_ = (data.get("metaobjectDefinition") or {}).get("type") or type_
            self._types[key] = type_
        return self._types[key]

    async def entries(self, metaobject_type: str) -> dict[str, str]:
        """Existing entries of a taxonomy metaobject type: taxonomy value id -> entry id."""
        if metaobject_type not in self._entries:
            found: dict[str, str] = {}
            async for node in self.client.paginate(
                ENTRIES_QUERY, ["metaobjects"], {"type": metaobject_type}
            ):
                ref = next(
                    (f["value"] for f in node["fields"] if f["key"] == "taxonomy_reference"),
                    None,
                )
                if ref:
                    found[ref] = node["id"]
            self._entries[metaobject_type] = found
        return self._entries[metaobject_type]

    async def plan(self, requests: list[TaxonomyRequest]) -> list[TaxonomyItem]:
        products: dict[str, dict[str, Any] | None] = {}
        grouped: dict[tuple[str, str], TaxonomyItem] = {}
        raw: dict[tuple[str, str], list[Any]] = {}
        for req in requests:
            name = req.attribute.taxonomy_attribute
            if not name:
                continue
            group = (req.product_gid, name)
            item = grouped.get(group)
            if item is None:
                item = grouped[group] = TaxonomyItem(
                    suggestion_ids=[],
                    product_gid=req.product_gid,
                    product_title=req.product_title,
                    attribute_key=req.attribute.key,
                    taxonomy_attribute=name,
                )
            item.suggestion_ids.append(req.suggestion_id)
            raw.setdefault(group, []).extend(
                req.value if isinstance(req.value, list) else [req.value]
            )
        if not grouped:
            return []
        # Category metafields are a best-effort extra on top of the custom metafields: a Shopify
        # refusal here (for example a missing write_metaobjects scope) skips the item with the
        # reason instead of failing the publish. A revoked token still propagates.
        try:
            definitions = await self.definitions()
        except ShopifyAuthError:
            raise
        except ShopifyError as exc:
            for item in grouped.values():
                item.reason = f"Shopify refused the category metafield lookup: {exc}"
            return list(grouped.values())
        for group, item in grouped.items():
            try:
                if item.product_gid not in products:
                    data = await self.client.query(CATEGORY_QUERY, {"id": item.product_gid})
                    products[item.product_gid] = data.get("product")
                await self._plan_item(item, products[item.product_gid], raw[group], definitions)
            except ShopifyAuthError:
                raise
            except ShopifyError as exc:
                item.action, item.reason = "skipped", f"Shopify refused the lookup: {exc}"
        return list(grouped.values())

    async def _plan_item(
        self,
        item: TaxonomyItem,
        product: dict[str, Any] | None,
        values: list[Any],
        definitions: dict[str, dict[str, Any]],
    ) -> None:
        if product is None:
            item.reason = "the product no longer exists in Shopify"
            return
        item.product_title = product.get("title") or item.product_title
        category = product.get("category")
        if not category:
            item.reason = "the product has no Shopify category; set one to use category metafields"
            return
        attribute = find_attribute(category, item.taxonomy_attribute)
        if attribute is None:
            item.reason = (
                f"the category {category.get('fullName')!r} has no "
                f"{item.taxonomy_attribute!r} attribute"
            )
            return
        choices = (attribute.get("values") or {}).get("nodes") or []
        item.values, item.unmatched = match_values(values, choices)
        if not item.values:
            item.reason = f"no taxonomy value of {attribute['name']!r} matches"
            return
        definition = definitions.get(_norm(attribute["name"]))
        if definition is None:
            item.reason = (
                f"the store has no {attribute['name']!r} category metafield yet; pick a value "
                "for it once in Shopify admin to enable it"
            )
            return
        item.key = definition["key"]
        item.metaobject_type = await self._metaobject_type(definition)
        existing = next(
            (
                m
                for m in (product.get("metafields") or {}).get("nodes") or []
                if m["namespace"] == NAMESPACE and m["key"] == item.key
            ),
            None,
        )
        if existing is not None:
            if existing["type"] != LIST_TYPE:
                item.reason = f"{item.metafield} has unexpected type {existing['type']}"
                return
            item.current_value, item.compare_digest = existing["value"], existing["compareDigest"]
        entries = await self.entries(item.metaobject_type)
        item.entry_ids = {v["id"]: entries[v["id"]] for v in item.values if v["id"] in entries}
        current = set(item.current_ids())
        known = set(item.entry_ids.values())
        complete = len(item.entry_ids) == len(item.values)
        item.action = "unchanged" if complete and known <= current else "add"

    async def _create_entry(self, metaobject_type: str, value: dict[str, str]) -> str:
        data = await self.client.query(
            CREATE_ENTRY_MUTATION,
            {
                "metaobject": {
                    "type": metaobject_type,
                    "fields": [
                        {"key": "label", "value": value["name"]},
                        {"key": "taxonomy_reference", "value": value["id"]},
                    ],
                }
            },
        )
        result = data["metaobjectCreate"]
        errors = result.get("userErrors") or []
        if errors or not result.get("metaobject"):
            raise TaxonomyError("; ".join(e["message"] for e in errors) or "entry not created")
        entry_id: str = result["metaobject"]["id"]
        self._entries.setdefault(metaobject_type, {})[value["id"]] = entry_id
        return entry_id

    async def publish(self, items: list[TaxonomyItem]) -> list[TaxonomyOutcome]:
        outcomes: list[TaxonomyOutcome] = []
        for item in items:
            if item.action != "add":
                outcomes.append(TaxonomyOutcome(item, ok=item.action == "unchanged"))
                continue
            assert item.key and item.metaobject_type
            try:
                for value in item.values:
                    if value["id"] not in item.entry_ids:
                        item.entry_ids[value["id"]] = await self._create_entry(
                            item.metaobject_type, value
                        )
            except ShopifyAuthError:
                raise
            except (TaxonomyError, ShopifyError) as exc:
                outcomes.append(
                    TaxonomyOutcome(item, ok=False, error=f"could not create entry: {exc}")
                )
                continue
            ids = item.current_ids()
            ids += [e for e in item.entry_ids.values() if e not in ids]
            after = json.dumps(ids)
            entry: dict[str, Any] = {
                "ownerId": item.product_gid,
                "namespace": NAMESPACE,
                "key": item.key,
                "type": LIST_TYPE,
                "value": after,
            }
            if item.compare_digest:
                entry["compareDigest"] = item.compare_digest
            try:
                data = await self.client.query(SET_MUTATION, {"metafields": [entry]})
            except ShopifyAuthError:
                raise
            except ShopifyError as exc:
                outcomes.append(TaxonomyOutcome(item, ok=False, error=str(exc)))
                continue
            errors = data["metafieldsSet"].get("userErrors") or []
            if errors:
                outcomes.append(
                    TaxonomyOutcome(item, ok=False, error="; ".join(e["message"] for e in errors))
                )
            else:
                outcomes.append(
                    TaxonomyOutcome(item, ok=True, before=item.current_value, after=after)
                )
        return outcomes


class TaxonomyError(Exception):
    pass
