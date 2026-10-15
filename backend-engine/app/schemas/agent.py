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


class TransactionOut(BaseModel):
    id: uuid.UUID
    protocol: str
    agent_name: str | None
    status: str
    amount: Decimal
    currency: str
    payment_ref: str | None
    order_gid: str | None
    order_name: str | None
    admin_url: str | None
    error_code: str | None
    error_detail: str | None
    created_at: datetime


class TransactionPage(BaseModel):
    total: int
    items: list[TransactionOut]


class PaymentSettings(BaseModel):
    stripe_account_id: str | None = Field(default=None, pattern=r"^acct_[A-Za-z0-9]+$")


class PaymentSettingsOut(PaymentSettings):
    stripe_configured: bool
    test_mode: bool
