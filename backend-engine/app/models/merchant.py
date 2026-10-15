import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Column, LargeBinary, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

from app.models.base import pg_enum, tz_column, utcnow


class MerchantStatus(StrEnum):
    ACTIVE = "active"
    UNINSTALLED = "uninstalled"


class AgentProtocol(StrEnum):
    UCP = "ucp"
    ACP = "acp"
    MCP = "mcp"


class Merchant(SQLModel, table=True):
    __tablename__ = "merchants"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    shop_domain: str = Field(sa_column=Column(String, unique=True, nullable=False, index=True))
    access_token_enc: bytes | None = Field(default=None, sa_column=Column(LargeBinary))
    scopes: str = ""
    status: MerchantStatus = Field(
        default=MerchantStatus.ACTIVE,
        sa_column=Column(pg_enum(MerchantStatus, "merchant_status"), nullable=False),
    )
    installed_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    uninstalled_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
    settings: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )


class AgentClient(SQLModel, table=True):
    """An AI agent / platform allowed to call this merchant's protocol endpoints."""

    __tablename__ = "agent_clients"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    protocol: AgentProtocol = Field(
        sa_column=Column(pg_enum(AgentProtocol, "agent_protocol"), nullable=False)
    )
    name: str
    key_prefix: str = Field(index=True, unique=True)
    key_hash: str
    scopes: list[str] = Field(
        default_factory=list, sa_column=Column(ARRAY(String), nullable=False, server_default="{}")
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    revoked_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
