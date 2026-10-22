"""Merchant management of the API keys AI agents use for UCP / ACP / MCP (called by frontend-ui)."""

import uuid

import anyio
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentMerchant, SessionDep, require_service_scopes
from app.models import AgentClient, AgentProtocol
from app.repositories.agent_clients import AgentClientRepository
from app.schemas.agent import AgentClientCreated, AgentClientIn, AgentClientOut
from app.services.agent_keys import issue_key

router = APIRouter(prefix="/merchant/agent-clients", tags=["agent-clients"])
write = [Depends(require_service_scopes("merchant:write"))]

MAX_ACTIVE_KEYS = 20


def _out(client: AgentClient) -> AgentClientOut:
    return AgentClientOut(
        id=client.id,
        name=client.name,
        protocol=client.protocol.value,
        key_prefix=client.key_prefix,
        scopes=list(client.scopes),
        created_at=client.created_at,
        revoked_at=client.revoked_at,
    )


@router.get("", response_model=list[AgentClientOut])
async def list_agent_clients(
    merchant: CurrentMerchant, session: SessionDep
) -> list[AgentClientOut]:
    """List the merchant's agent API keys (without secrets)."""
    return [_out(c) for c in await AgentClientRepository(session).list_for(merchant.id)]


@router.post(
    "", response_model=AgentClientCreated, status_code=status.HTTP_201_CREATED, dependencies=write
)
async def create_agent_client(
    body: AgentClientIn, merchant: CurrentMerchant, session: SessionDep
) -> AgentClientCreated:
    """Issue a new agent API key. The plaintext key is returned once and only its hash is stored."""
    repo = AgentClientRepository(session)
    if await repo.count_active(merchant.id) >= MAX_ACTIVE_KEYS:
        raise HTTPException(status.HTTP_409_CONFLICT, "too many active keys; revoke one first")
    key = await anyio.to_thread.run_sync(issue_key)  # argon2 hashing is CPU-bound
    client = await repo.create(
        merchant.id, body.name, AgentProtocol(body.protocol), sorted(set(body.scopes)), key
    )
    return AgentClientCreated(**_out(client).model_dump(), api_key=key.plaintext)


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=write)
async def revoke_agent_client(
    client_id: uuid.UUID, merchant: CurrentMerchant, session: SessionDep
) -> None:
    """Revoke one of the merchant's agent API keys."""
    if await AgentClientRepository(session).revoke(merchant.id, client_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent client not found")
