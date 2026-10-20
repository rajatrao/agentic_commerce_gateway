"""Category schemas: which structured attributes a kind of product should have.

A schema lists attribute definitions (type, unit, importance, vocabulary, default Shopify
metafield) plus the variant options and representative shopping queries for its category.
Built-in schemas cover outdoor gear; merchants can edit them or add schemas for other verticals
(stored as `category_schemas` overrides and validated with `CategorySchema`).

Attribute values use one JSON shape per type, everywhere (snapshots, suggestions, approvals):

    text        "Ripstop nylon"
    list        ["hiking", "travel"]
    number      12.5                      (the unit, if any, is fixed by the definition)
    boolean     true
    weight      {"value": 1.2, "unit": "kg"}      units: g kg oz lb
    volume      {"value": 28, "unit": "l"}        units: ml l
    dimension   {"value": 40, "unit": "cm"}       units: mm cm m in

`parse_value` turns free text or Shopify metafield values into that shape and `validate_value`
checks one against its definition. Neither ever guesses: unparseable input is an error.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

AttributeType = Literal["text", "list", "number", "boolean", "weight", "volume", "dimension"]
Importance = Literal["critical", "high", "medium", "low"]
MEASUREMENT_TYPES = {"weight", "volume", "dimension"}

IMPORTANCE_WEIGHT: dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}

# Unit spellings -> canonical unit, and canonical unit -> factor to the type's base unit
# (g, l, cm). Used for parsing and for comparing values written in different units.
UNIT_ALIASES: dict[str, dict[str, str]] = {
    "weight": {
        "g": "g", "gram": "g", "grams": "g", "gr": "g",
        "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
        "oz": "oz", "ounce": "oz", "ounces": "oz",
        "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    },
    "volume": {
        "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l", "ltr": "l",
        "ml": "ml", "milliliter": "ml", "milliliters": "ml", "millilitre": "ml",
    },
    "dimension": {
        "mm": "mm", "millimeter": "mm", "millimeters": "mm",
        "cm": "cm", "centimeter": "cm", "centimeters": "cm",
        "m": "m", "meter": "m", "meters": "m", "metre": "m",
        "in": "in", "inch": "in", "inches": "in", '"': "in",
    },
}  # fmt: skip
UNIT_FACTORS: dict[str, dict[str, Decimal]] = {
    "weight": {
        "g": Decimal(1), "kg": Decimal(1000), "oz": Decimal("28.3495"), "lb": Decimal("453.592"),
    },
    "volume": {"ml": Decimal("0.001"), "l": Decimal(1)},
    "dimension": {"mm": Decimal("0.1"), "cm": Decimal(1), "m": Decimal(100), "in": Decimal("2.54")},
}  # fmt: skip
# Shopify's typed metafields spell some units differently.
SHOPIFY_UNITS = {"weight": {"g": "g", "kg": "kg", "oz": "oz", "lb": "lb"}}

NUMBER_RE = r"(\d+(?:[.,]\d+)?)"
KEY_RE = r"^[a-z][a-z0-9_]{1,63}$"


class MetafieldTarget(BaseModel):
    namespace: str = Field(pattern=r"^[a-zA-Z0-9_-]{2,255}$")
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{2,64}$")
    type: str = Field(min_length=3, max_length=64)


class AttributeDefinition(BaseModel):
    key: str = Field(pattern=KEY_RE)
    label: str = Field(min_length=1, max_length=80)
    type: AttributeType
    unit: str | None = Field(default=None, max_length=16)
    importance: Importance = "medium"
    required: bool = False
    allowed_values: list[str] | None = None
    aliases: list[str] = Field(default_factory=list, max_length=20)
    # Optional regex with one capture group, used by the rule-based extractor for `number`.
    pattern: str | None = Field(default=None, max_length=300)
    why: str = Field(default="", max_length=400)
    metafield: MetafieldTarget | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            compiled = re.compile(v, re.IGNORECASE)
            if compiled.groups != 1:
                raise ValueError("pattern needs exactly one capture group")
        return v

    @model_validator(mode="after")
    def _unit_fits(self) -> AttributeDefinition:
        if self.type in MEASUREMENT_TYPES:
            self.unit = self.unit or {"weight": "g", "volume": "l", "dimension": "cm"}[self.type]
            if self.unit not in UNIT_FACTORS[self.type]:
                raise ValueError(f"unit {self.unit!r} is not a {self.type} unit")
        return self


class DiscoveryQuery(BaseModel):
    """A shopping question an agent might get, and the attributes needed to answer it."""

    query: str = Field(min_length=3, max_length=300)
    attributes: list[str] = Field(min_length=1, max_length=10)


class CategorySchema(BaseModel):
    key: str = Field(pattern=KEY_RE)
    name: str = Field(min_length=1, max_length=80)
    # How products are assigned to this schema (case-insensitive substring matches).
    match_product_types: list[str] = Field(default_factory=list)
    match_keywords: list[str] = Field(default_factory=list)
    match_categories: list[str] = Field(default_factory=list)
    variant_options: list[str] = Field(default_factory=list)
    attributes: list[AttributeDefinition] = Field(min_length=1, max_length=40)
    discovery_queries: list[DiscoveryQuery] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _consistent(self) -> CategorySchema:
        keys = [a.key for a in self.attributes]
        if len(keys) != len(set(keys)):
            raise ValueError("attribute keys must be unique")
        for q in self.discovery_queries:
            unknown = set(q.attributes) - set(keys)
            if unknown:
                raise ValueError(f"discovery query uses unknown attributes: {sorted(unknown)}")
        return self

    def attribute(self, key: str) -> AttributeDefinition | None:
        return next((a for a in self.attributes if a.key == key), None)


# --------------------------------------------------------------------------- built-ins


def _mf(key: str, type_: str) -> dict[str, str]:
    return {"namespace": "custom", "key": key, "type": type_}


MATERIALS = [
    "ripstop nylon", "nylon", "polyester", "cordura", "dyneema", "gore-tex", "merino wool",
    "wool", "cotton", "canvas", "leather", "suede", "mesh", "down", "synthetic insulation",
    "aluminum", "aluminium", "carbon fiber", "titanium", "stainless steel", "silicone",
    "recycled polyester", "recycled nylon", "polyurethane", "eva foam", "rubber",
]  # fmt: skip
USES = [
    "hiking", "backpacking", "camping", "trekking", "mountaineering", "climbing",
    "trail running", "running", "travel", "commuting", "everyday", "skiing", "snowboarding",
    "cycling", "fishing", "hunting", "kayaking", "car camping", "thru-hiking",
]  # fmt: skip

_COMMON: dict[str, dict[str, Any]] = {
    "material": {
        "key": "material", "label": "Material", "type": "text", "importance": "high",
        "required": True, "allowed_values": MATERIALS,
        "why": "Shoppers filter and compare on material (durability, weight, care).",
        "metafield": _mf("material", "single_line_text_field"),
    },
    "weight": {
        "key": "weight", "label": "Weight", "type": "weight", "unit": "g", "importance": "high",
        "required": True, "aliases": ["weight", "weighs", "weighing"],
        "why": "Weight is a top comparison point for carried gear.",
        "metafield": _mf("weight", "weight"),
    },
    "intended_use": {
        "key": "intended_use", "label": "Intended use", "type": "list", "importance": "high",
        "required": True, "allowed_values": USES,
        "why": "Agents match products to activities ('a pack for a 3-day hike').",
        "metafield": _mf("intended_use", "list.single_line_text_field"),
    },
    "care_instructions": {
        "key": "care_instructions", "label": "Care instructions", "type": "text",
        "importance": "low", "aliases": ["machine wash", "hand wash", "wash", "dry clean", "care"],
        "why": "Answers common post-purchase and pre-purchase questions.",
        "metafield": _mf("care_instructions", "multi_line_text_field"),
    },
    "water_resistance": {
        "key": "water_resistance", "label": "Water resistance", "type": "text",
        "importance": "medium",
        "allowed_values": [
            "waterproof", "water-resistant", "water repellent", "not water resistant",
        ],
        "why": "Weather protection claims need to be explicit and verifiable.",
        "metafield": _mf("water_resistance", "single_line_text_field"),
    },
    "waterproof_rating": {
        "key": "waterproof_rating", "label": "Waterproof rating (mm)", "type": "number",
        "unit": "mm", "importance": "medium", "pattern": r"(\d{3,5})\s*mm",
        "why": "A hydrostatic head rating backs up a waterproof claim.",
        "metafield": _mf("waterproof_rating_mm", "number_integer"),
    },
}  # fmt: skip


def _attrs(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(i) for i in items]


BUILTIN_SCHEMAS: list[dict[str, Any]] = [
    {
        "key": "backpacks",
        "name": "Backpacks",
        "match_product_types": ["backpack", "pack", "daypack", "rucksack"],
        "match_keywords": ["backpack", "daypack", "rucksack", "hiking pack"],
        "match_categories": ["backpacks"],
        "attributes": _attrs(
            {
                "key": "capacity",
                "label": "Capacity",
                "type": "volume",
                "unit": "l",
                "importance": "critical",
                "required": True,
                "aliases": ["capacity", "volume"],
                "why": "Capacity is the first thing shoppers and agents filter backpacks on.",
                "metafield": _mf("capacity_liters", "number_decimal"),
            },
            _COMMON["weight"],
            _COMMON["material"],
            _COMMON["intended_use"],
            {
                "key": "laptop_compartment",
                "label": "Laptop compartment",
                "type": "boolean",
                "importance": "medium",
                "aliases": ["laptop compartment", "laptop sleeve", "laptop pocket"],
                "why": "Commuters and travellers ask for it explicitly.",
                "metafield": _mf("laptop_compartment", "boolean"),
            },
            {
                "key": "max_laptop_size",
                "label": "Max laptop size (in)",
                "type": "number",
                "unit": "in",
                "importance": "low",
                "pattern": r"(?:up to|fits)\s*(?:a\s*)?(\d{2}(?:\.\d)?)\s*(?:\"|-?\s*in(?:ch)?)",
                "why": "Agents need it to confirm a specific laptop fits.",
                "metafield": _mf("max_laptop_size_in", "number_decimal"),
            },
            _COMMON["water_resistance"],
            {
                "key": "carry_on_compatible",
                "label": "Carry-on compatible",
                "type": "boolean",
                "importance": "low",
                "aliases": ["carry-on", "carry on compatible", "cabin size"],
                "why": "A frequent travel question that needs a yes/no answer.",
                "metafield": _mf("carry_on_compatible", "boolean"),
            },
            _COMMON["care_instructions"],
        ),
        "discovery_queries": [
            {
                "query": "lightweight hiking backpack around 30 liters",
                "attributes": ["capacity", "weight", "intended_use"],
            },
            {
                "query": "travel backpack with a laptop compartment",
                "attributes": ["intended_use", "laptop_compartment"],
            },
            {
                "query": "waterproof daypack made of ripstop nylon",
                "attributes": ["water_resistance", "material"],
            },
        ],
    },
    {
        "key": "tents",
        "name": "Tents",
        "match_product_types": ["tent", "shelter"],
        "match_keywords": ["tent", "shelter", "bivy"],
        "match_categories": ["tents"],
        "attributes": _attrs(
            {
                "key": "sleeping_capacity",
                "label": "Sleeping capacity (people)",
                "type": "number",
                "unit": "people",
                "importance": "critical",
                "required": True,
                "pattern": r"(\d)\s*-?\s*(?:person|people|p)\b",
                "why": "Agents answer 'a tent for two' from this value.",
                "metafield": _mf("sleeping_capacity", "number_integer"),
            },
            _COMMON["weight"],
            {
                "key": "seasons",
                "label": "Season rating",
                "type": "text",
                "importance": "high",
                "required": True,
                "allowed_values": ["3-season", "4-season", "3+ season", "summer"],
                "why": "Season rating decides whether a tent suits the trip.",
                "metafield": _mf("season_rating", "single_line_text_field"),
            },
            _COMMON["material"],
            _COMMON["waterproof_rating"],
            {
                "key": "packed_size",
                "label": "Packed length",
                "type": "dimension",
                "unit": "cm",
                "importance": "low",
                "aliases": ["packed size", "packed length", "packs down"],
                "why": "Backpackers check whether it fits their pack.",
                "metafield": _mf("packed_length", "dimension"),
            },
            _COMMON["intended_use"],
        ),
        "discovery_queries": [
            {
                "query": "ultralight 2 person backpacking tent",
                "attributes": ["sleeping_capacity", "weight", "intended_use"],
            },
            {
                "query": "4-season tent for winter camping",
                "attributes": ["seasons", "intended_use"],
            },
        ],
    },
    {
        "key": "sleeping_bags",
        "name": "Sleeping bags",
        "match_product_types": ["sleeping bag", "quilt"],
        "match_keywords": ["sleeping bag", "quilt"],
        "match_categories": ["sleeping bags"],
        "attributes": _attrs(
            {
                "key": "temperature_rating",
                "label": "Temperature rating (°F)",
                "type": "number",
                "unit": "°F",
                "importance": "critical",
                "required": True,
                "pattern": r"(-?\d{1,2})\s*°?\s*F\b",
                "why": "The temperature rating is how shoppers choose a bag.",
                "metafield": _mf("temperature_rating_f", "number_integer"),
            },
            {
                "key": "fill_type",
                "label": "Fill type",
                "type": "text",
                "importance": "high",
                "required": True,
                "allowed_values": ["down", "synthetic"],
                "why": "Down vs synthetic drives warmth, weight and wet performance.",
                "metafield": _mf("fill_type", "single_line_text_field"),
            },
            _COMMON["weight"],
            {
                "key": "packed_size",
                "label": "Packed length",
                "type": "dimension",
                "unit": "cm",
                "importance": "low",
                "aliases": ["packed size", "packed length", "stuff sack"],
                "why": "Determines whether it fits the shopper's pack.",
                "metafield": _mf("packed_length", "dimension"),
            },
            _COMMON["intended_use"],
        ),
        "discovery_queries": [
            {
                "query": "down sleeping bag rated to 20 degrees",
                "attributes": ["fill_type", "temperature_rating"],
            },
        ],
    },
    {
        "key": "outerwear",
        "name": "Jackets and outerwear",
        "match_product_types": ["jacket", "shell", "parka", "vest", "outerwear", "coat"],
        "match_keywords": ["jacket", "shell", "parka", "anorak", "windbreaker"],
        "match_categories": ["coats & jackets", "outerwear"],
        "variant_options": ["Size"],
        "attributes": _attrs(
            _COMMON["material"],
            _COMMON["water_resistance"],
            _COMMON["waterproof_rating"],
            {
                "key": "insulation",
                "label": "Insulation",
                "type": "text",
                "importance": "medium",
                "allowed_values": ["down", "synthetic", "fleece", "none"],
                "why": "Agents match insulated vs shell jackets to conditions.",
                "metafield": _mf("insulation", "single_line_text_field"),
            },
            _COMMON["weight"] | {"required": False, "importance": "medium"},
            _COMMON["intended_use"],
            _COMMON["care_instructions"],
        ),
        "discovery_queries": [
            {
                "query": "waterproof hiking rain jacket",
                "attributes": ["water_resistance", "intended_use"],
            },
            {"query": "insulated jacket for skiing", "attributes": ["insulation", "intended_use"]},
        ],
    },
    {
        "key": "footwear",
        "name": "Footwear",
        "match_product_types": ["boot", "shoe", "sandal", "footwear"],
        "match_keywords": ["boot", "shoe", "sandal", "trail runner"],
        "match_categories": ["shoes", "footwear"],
        "variant_options": ["Size"],
        "attributes": _attrs(
            _COMMON["material"],
            _COMMON["water_resistance"],
            _COMMON["weight"] | {"required": False, "importance": "medium"},
            {
                "key": "heel_drop",
                "label": "Heel-to-toe drop (mm)",
                "type": "number",
                "unit": "mm",
                "importance": "low",
                "pattern": r"(\d{1,2})\s*mm\s*(?:drop|heel)",
                "why": "Runners compare drop when switching shoes.",
                "metafield": _mf("heel_drop_mm", "number_integer"),
            },
            _COMMON["intended_use"],
        ),
        "discovery_queries": [
            {
                "query": "waterproof hiking boots",
                "attributes": ["water_resistance", "intended_use"],
            },
        ],
    },
    {
        "key": "general",
        "name": "General products",
        "attributes": _attrs(
            _COMMON["material"] | {"importance": "medium"},
            _COMMON["weight"] | {"required": False, "importance": "medium"},
            _COMMON["intended_use"] | {"importance": "medium"},
            _COMMON["care_instructions"],
        ),
        "discovery_queries": [],
    },
]

FALLBACK_SCHEMA_KEY = "general"


def builtin_schemas() -> dict[str, CategorySchema]:
    return {s["key"]: CategorySchema.model_validate(s) for s in BUILTIN_SCHEMAS}


def merge_schemas(overrides: dict[str, dict[str, Any]]) -> dict[str, CategorySchema]:
    """Built-ins with the merchant's overrides (and added schemas) applied."""
    schemas = builtin_schemas()
    for key, definition in overrides.items():
        try:
            schemas[key] = CategorySchema.model_validate({**definition, "key": key})
        except ValueError:
            continue  # validated on write; a stale row must not break audits
    return schemas


def match_schema(product: dict[str, Any], schemas: dict[str, CategorySchema]) -> CategorySchema:
    """Pick the schema for a product: Shopify category first, then product type, then title."""
    category = ((product.get("category") or {}).get("fullName") or "").lower()
    product_type = (product.get("productType") or product.get("product_type") or "").lower()
    title = (product.get("title") or "").lower()
    ordered = [s for k, s in schemas.items() if k != FALLBACK_SCHEMA_KEY]
    for field, attr in ((category, "match_categories"), (product_type, "match_product_types")):
        if field:
            for schema in ordered:
                if any(term.lower() in field for term in getattr(schema, attr)):
                    return schema
    for schema in ordered:
        if any(re.search(rf"\b{re.escape(t.lower())}", title) for t in schema.match_keywords):
            return schema
    return schemas.get(FALLBACK_SCHEMA_KEY) or next(iter(schemas.values()))


# --------------------------------------------------------------------------- values


class InvalidValue(ValueError):
    """Raised by `parse_value` when input cannot be read as the attribute's type."""


def _decimal(raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw).replace(",", ".").strip())
    except (InvalidOperation, ValueError) as exc:
        raise InvalidValue(f"{raw!r} is not a number") from exc
    if not value.is_finite():
        raise InvalidValue(f"{raw!r} is not a number")
    return value


def _num(value: Decimal) -> float | int:
    return int(value) if value == value.to_integral_value() else float(value)


def parse_measurement(raw: Any, kind: str) -> dict[str, Any]:
    """'28 L', '1.2kg', {"value": 2, "unit": "lb"}, or Shopify's weight JSON -> canonical."""
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidValue(f"invalid {kind} JSON") from exc
    if isinstance(raw, dict):
        value, unit = raw.get("value"), str(raw.get("unit") or "").lower()
    else:
        match = re.fullmatch(rf"\s*{NUMBER_RE}\s*([a-zA-Z\"]+)\s*", str(raw))
        if not match:
            raise InvalidValue(f"{raw!r} needs a number and a {kind} unit")
        value, unit = match.group(1), match.group(2).lower()
    canonical = UNIT_ALIASES[kind].get(unit.lower().replace("_", ""))
    if canonical is None:
        raise InvalidValue(f"{unit!r} is not a {kind} unit")
    number = _decimal(value)
    if number <= 0:
        raise InvalidValue(f"{kind} must be positive")
    return {"value": _num(number), "unit": canonical}


def to_base(value: dict[str, Any], kind: str) -> Decimal:
    return Decimal(str(value["value"])) * UNIT_FACTORS[kind][value["unit"]]


def convert(value: dict[str, Any], kind: str, unit: str) -> Decimal:
    return to_base(value, kind) / UNIT_FACTORS[kind][unit]


def parse_value(raw: Any, attr: AttributeDefinition) -> Any:
    """Read `raw` (text, JSON, metafield value) as `attr`'s canonical value or raise."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise InvalidValue("empty value")
    t = attr.type
    if t in MEASUREMENT_TYPES:
        if isinstance(raw, int | float | Decimal) or (
            isinstance(raw, str) and re.fullmatch(rf"\s*{NUMBER_RE}\s*", raw)
        ):
            # A bare number is only unambiguous when the definition fixes the unit.
            return {"value": _num(_decimal(raw)), "unit": attr.unit}
        return parse_measurement(raw, t)
    if t == "number":
        if isinstance(raw, bool):
            raise InvalidValue("not a number")
        if isinstance(raw, str):
            match = re.fullmatch(rf"\s*(-?{NUMBER_RE[1:-1]})\s*(\S*)\s*", raw)
            if not match:
                raise InvalidValue(f"{raw!r} is not a number")
            raw = match.group(1)
        return _num(_decimal(raw))
    if t == "boolean":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "yes", "y", "1"):
            return True
        if text in ("false", "no", "n", "0"):
            return False
        raise InvalidValue(f"{raw!r} is not yes/no")
    if t == "list":
        if isinstance(raw, str):
            stripped = raw.strip()
            if stripped.startswith("["):
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise InvalidValue("invalid list JSON") from exc
            else:
                raw = [p for p in re.split(r"[,;\n]", stripped)]
        if not isinstance(raw, list):
            raise InvalidValue("not a list")
        items = [str(i).strip() for i in raw if str(i).strip()]
        if not items:
            raise InvalidValue("empty list")
        return list(dict.fromkeys(items))
    text = str(raw).strip()
    if len(text) > 2000:
        raise InvalidValue("text longer than 2000 characters")
    return text


def validate_value(value: Any, attr: AttributeDefinition) -> list[str]:
    """Problems with an already-parsed value (empty list = valid)."""
    try:
        parsed = parse_value(value, attr)
    except InvalidValue as exc:
        return [str(exc)]
    errors: list[str] = []
    if attr.allowed_values:
        allowed = {v.lower() for v in attr.allowed_values}
        items = parsed if isinstance(parsed, list) else [parsed]
        if attr.type in ("text", "list"):
            unknown = [i for i in items if i.lower() not in allowed]
            # Free-text materials like "ripstop nylon with PU coating" contain a known term.
            unknown = [i for i in unknown if not any(a in i.lower() for a in allowed)]
            if unknown:
                errors.append(f"not in the schema vocabulary: {', '.join(unknown)}")
    if attr.type == "number" and attr.key.endswith(("capacity", "size", "rating")):
        if isinstance(parsed, int | float) and parsed < 0 and attr.unit not in ("°F", "°C"):
            errors.append("must not be negative")
    return errors


def values_equal(a: Any, b: Any, attr: AttributeDefinition) -> bool:
    """Compare two canonical values, converting measurement units (2% tolerance)."""
    if a is None or b is None:
        return a is b
    try:
        a, b = parse_value(a, attr), parse_value(b, attr)
    except InvalidValue:
        return False
    if attr.type in MEASUREMENT_TYPES:
        x, y = to_base(a, attr.type), to_base(b, attr.type)
        return abs(x - y) <= max(x, y) * Decimal("0.02")
    if attr.type == "number":
        return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal("0.01")
    if attr.type == "list":
        return {i.lower() for i in a} == {i.lower() for i in b}
    if attr.type == "text":
        return str(a).strip().lower() == str(b).strip().lower()
    return bool(a == b)


def display_value(value: Any, attr: AttributeDefinition | None = None) -> str:
    if value is None:
        return "Unknown"
    if isinstance(value, dict) and "value" in value:
        return f"{value['value']} {value.get('unit', '')}".strip()
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if attr is not None and attr.type == "number" and attr.unit:
        return f"{value} {attr.unit}"
    return str(value)
