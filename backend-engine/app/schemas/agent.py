import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

AgentScope = Literal["catalog:read", "checkout"]


def _all_scopes() -> list[AgentScope]:
    return ["catalog:read", "checkout"]


class AgentClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    protocol: Literal["ucp", "acp", "mcp"]
    scopes: list[AgentScope] = Field(default_factory=_all_scopes, min_length=1)


class AgentClientOut(BaseModel):
    id: uuid.UUID
    name: str
    protocol: str
    key_prefix: str
    scopes: list[str]
    created_at: datetime
    revoked_at: datetime | None


class AgentClientCreated(AgentClientOut):
    api_key: str
    """Shown once. Only an argon2 hash is stored."""


class AgentCheckoutOut(BaseModel):
    """A cart an agent built for this store. Ready carts link to the store's own checkout."""

    id: uuid.UUID
    protocol: str
    agent_name: str | None
    status: str
    item_count: int
    total: Decimal
    currency: str
    checkout_url: str | None
    # Set once the buyer places the order on Shopify checkout.
    order_name: str | None
    order_admin_url: str | None
    ordered_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentCheckoutPage(BaseModel):
    total: int
    # How many of this store's agent carts became Shopify orders.
    ordered: int
    items: list[AgentCheckoutOut]
