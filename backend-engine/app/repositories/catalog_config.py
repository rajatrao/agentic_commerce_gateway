"""Per-merchant catalog configuration: category schemas, metafield mappings, scoring settings."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import CategorySchemaOverride, Merchant, MetafieldMapping
from app.models.base import utcnow
from app.services.catalog_schema import CategorySchema, merge_schemas
from app.services.readiness import READY_THRESHOLD, normalize_weights


@dataclass(frozen=True)
class MappingTarget:
    """The metafield an attribute is written to, and whether that comes from the schema or the
    merchant."""

    attribute_key: str
    namespace: str
    key: str
    type: str
    enabled: bool
    source: str  # "default" (from the schema) or "custom" (merchant-configured)


@dataclass
class CatalogConfig:
    """A merchant's effective catalog configuration: schemas, metafield mappings and readiness
    settings."""

    schemas: dict[str, CategorySchema]
    mappings: dict[str, MappingTarget]
    weights: dict[str, int]
    ready_threshold: int

    def mapping_keys(self) -> dict[str, tuple[str, str]]:
        """Return each attribute's metafield (namespace, key)."""
        return {k: (m.namespace, m.key) for k, m in self.mappings.items()}


def catalog_settings(merchant: Merchant) -> dict[str, Any]:
    """Return the merchant's readiness weights and ready threshold, with defaults filled in."""
    raw = merchant.settings.get("catalog") or {}
    return {
        "readiness_weights": normalize_weights(raw.get("readiness_weights")),
        "ready_threshold": int(raw.get("ready_threshold", READY_THRESHOLD)),
    }


class CatalogConfigRepository:
    """Loads and saves a merchant's schema overrides, metafield mappings and catalog settings."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def overrides(self, merchant_id: uuid.UUID) -> Sequence[CategorySchemaOverride]:
        """Return the merchant's custom schema definitions."""
        return (
            await self.session.exec(
                select(CategorySchemaOverride).where(
                    CategorySchemaOverride.merchant_id == merchant_id
                )
            )
        ).all()

    async def schemas(self, merchant_id: uuid.UUID) -> dict[str, CategorySchema]:
        """Return the built-in schemas merged with the merchant's overrides."""
        return merge_schemas({o.key: o.definition for o in await self.overrides(merchant_id)})

    async def save_schema(self, merchant_id: uuid.UUID, schema: CategorySchema) -> None:
        """Create or replace the merchant's override of a schema."""
        row = (
            await self.session.exec(
                select(CategorySchemaOverride).where(
                    CategorySchemaOverride.merchant_id == merchant_id,
                    CategorySchemaOverride.key == schema.key,
                )
            )
        ).first()
        definition = schema.model_dump(mode="json")
        if row is None:
            row = CategorySchemaOverride(
                merchant_id=merchant_id, key=schema.key, definition=definition
            )
        else:
            row.definition, row.updated_at = definition, utcnow()
            flag_modified(row, "definition")
        self.session.add(row)
        await self.session.commit()

    async def reset_schema(self, merchant_id: uuid.UUID, key: str) -> bool:
        """Delete the merchant's override of a schema. Returns whether one existed."""
        result = await self.session.exec(
            delete(CategorySchemaOverride).where(
                col(CategorySchemaOverride.merchant_id) == merchant_id,
                col(CategorySchemaOverride.key) == key,
            )
        )
        await self.session.commit()
        return bool(result.rowcount)

    async def mappings(
        self, merchant_id: uuid.UUID, schemas: dict[str, CategorySchema] | None = None
    ) -> dict[str, MappingTarget]:
        """Every attribute's write target: the merchant's mapping, else the schema default."""
        schemas = schemas or await self.schemas(merchant_id)
        out: dict[str, MappingTarget] = {}
        for schema in schemas.values():
            for attr in schema.attributes:
                if attr.metafield and attr.key not in out:
                    out[attr.key] = MappingTarget(
                        attr.key,
                        attr.metafield.namespace,
                        attr.metafield.key,
                        attr.metafield.type,
                        True,
                        "default",
                    )
        rows = (
            await self.session.exec(
                select(MetafieldMapping).where(MetafieldMapping.merchant_id == merchant_id)
            )
        ).all()
        for row in rows:
            out[row.attribute_key] = MappingTarget(
                row.attribute_key, row.namespace, row.key, row.type, row.enabled, "custom"
            )
        return out

    async def save_mapping(
        self,
        merchant_id: uuid.UUID,
        attribute_key: str,
        namespace: str,
        key: str,
        type_: str,
        enabled: bool,
    ) -> None:
        """Create or replace the merchant's metafield mapping for an attribute."""
        row = (
            await self.session.exec(
                select(MetafieldMapping).where(
                    MetafieldMapping.merchant_id == merchant_id,
                    MetafieldMapping.attribute_key == attribute_key,
                )
            )
        ).first() or MetafieldMapping(
            merchant_id=merchant_id, attribute_key=attribute_key, namespace="", key="", type=""
        )
        row.namespace, row.key, row.type, row.enabled = namespace, key, type_, enabled
        row.updated_at = utcnow()
        self.session.add(row)
        await self.session.commit()

    async def load(self, merchant: Merchant) -> CatalogConfig:
        """Load the merchant's full effective catalog configuration."""
        schemas = await self.schemas(merchant.id)
        settings = catalog_settings(merchant)
        return CatalogConfig(
            schemas=schemas,
            mappings=await self.mappings(merchant.id, schemas),
            weights=settings["readiness_weights"],
            ready_threshold=settings["ready_threshold"],
        )

    async def save_settings(self, merchant: Merchant, values: dict[str, Any]) -> None:
        """Save the merchant's catalog settings."""
        merchant.settings = {**merchant.settings, "catalog": values}
        flag_modified(merchant, "settings")
        self.session.add(merchant)
        await self.session.commit()
