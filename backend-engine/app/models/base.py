"""Shared model helpers. All domain tables live in the `gateway` Postgres schema."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Column, DateTime, MetaData
from sqlalchemy import Enum as SAEnum
from sqlmodel import SQLModel

SCHEMA = "gateway"

# Deterministic constraint names keep Alembic autogenerate diffs stable.
SQLModel.metadata = MetaData(
    schema=SCHEMA,
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    },
)


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def tz_column(nullable: bool = False, **kwargs) -> Column:  # type: ignore[no-untyped-def,type-arg]
    """Build a `timestamptz` column."""
    return Column(DateTime(timezone=True), nullable=nullable, **kwargs)


def pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Build a Postgres enum type in the gateway schema that stores each member's value."""
    return SAEnum(
        enum_cls,
        name=name,
        schema=SCHEMA,
        values_callable=lambda e: [m.value for m in e],
    )
