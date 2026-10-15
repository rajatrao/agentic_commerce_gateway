import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.base import pg_enum


class MappingSource(StrEnum):
    TAG = "tag"
    METAFIELD = "metafield"
    PRODUCT_TYPE = "product_type"
    VENDOR = "vendor"


class MetaMappingRule(SQLModel, table=True):
    """Maps merchant tags/metafields onto schema.org or protocol properties."""

    __tablename__ = "meta_mapping_rules"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    source_type: MappingSource = Field(
        sa_column=Column(pg_enum(MappingSource, "mapping_source"), nullable=False)
    )
    source_key: str = ""
    match_pattern: str
    target_property: str
    transform: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )
    priority: int = 100
    active: bool = True
