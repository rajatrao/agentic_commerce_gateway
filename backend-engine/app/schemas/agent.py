"""Request and response models for agent API keys and the agent checkouts view."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

AgentScope = Literal["catalog:read", "checkout"]


def _all_scopes() -> list[AgentScope]:
    return ["catalog:read", "checkout"]


class AgentClientIn(BaseModel):
    """Request to issue an agent API key for one protocol and a set of scopes."""

    name: str = Field(min_length=1, max_length=80)
    protocol: Literal["ucp", "acp", "mcp"]
    scopes: list[AgentScope] = Field(default_factory=_all_scopes, min_length=1)


class AgentClientOut(BaseModel):
    """An agent API key as listed to the merchant, identified by its prefix."""

    id: uuid.UUID
    name: str
    protocol: str
    key_prefix: str
    scopes: list[str]
    created_at: datetime
    revoked_at: datetime | None


class AgentClientCreated(AgentClientOut):
    """A newly issued agent API key, including the plaintext key."""

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
    """A page of agent carts plus how many of them became Shopify orders."""

    total: int
    # How many of this store's agent carts became Shopify orders.
    ordered: int
    items: list[AgentCheckoutOut]
