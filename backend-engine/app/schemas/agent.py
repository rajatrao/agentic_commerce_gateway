import uuid
from datetime import datetime
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
