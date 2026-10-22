"""Agent API keys and the idempotency records of agent requests."""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import anyio
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import AgentClient, AgentProtocol, IdempotencyRecord, Merchant, MerchantStatus
from app.models.base import utcnow
from app.services.agent_keys import IssuedKey, KeyVerifier, key_prefix

IDEMPOTENCY_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class AgentPrincipal:
    """An authenticated agent: its API key record and the merchant it belongs to."""

    client: AgentClient
    merchant: Merchant


class AgentClientRepository:
    """Stores and authenticates agent API keys."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for(self, merchant_id: uuid.UUID) -> Sequence[AgentClient]:
        """List a merchant's agent keys, newest first, revoked ones included."""
        return (
            await self.session.exec(
                select(AgentClient)
                .where(AgentClient.merchant_id == merchant_id)
                .order_by(col(AgentClient.created_at).desc())
            )
        ).all()

    async def count_active(self, merchant_id: uuid.UUID) -> int:
        """Count a merchant's keys that are not revoked."""
        return len([c for c in await self.list_for(merchant_id) if c.revoked_at is None])

    async def create(
        self,
        merchant_id: uuid.UUID,
        name: str,
        protocol: AgentProtocol,
        scopes: list[str],
        key: IssuedKey,
    ) -> AgentClient:
        """Store a new agent key's prefix and hash."""
        client = AgentClient(
            merchant_id=merchant_id,
            name=name,
            protocol=protocol,
            scopes=scopes,
            key_prefix=key.prefix,
            key_hash=key.hash,
        )
        self.session.add(client)
        await self.session.commit()
        await self.session.refresh(client)
        return client

    async def revoke(self, merchant_id: uuid.UUID, client_id: uuid.UUID) -> AgentClient | None:
        """Revoke one of a merchant's keys. Returns None if the key is not the merchant's."""
        client = await self.session.get(AgentClient, client_id)
        if client is None or client.merchant_id != merchant_id:
            return None
        if client.revoked_at is None:
            client.revoked_at = utcnow()
            self.session.add(client)
            await self.session.commit()
        return client

    async def authenticate(self, plaintext: str, verifier: KeyVerifier) -> AgentPrincipal | None:
        """Resolve an agent API key to its active client and installed merchant."""
        prefix = key_prefix(plaintext)
        if prefix is None:
            return None
        row = (
            await self.session.exec(
                select(AgentClient, Merchant)
                .join(Merchant, col(Merchant.id) == col(AgentClient.merchant_id))
                .where(AgentClient.key_prefix == prefix)
            )
        ).first()
        if row is None:
            return None
        client, merchant = row
        if client.revoked_at is not None or merchant.status is not MerchantStatus.ACTIVE:
            return None
        # argon2 is deliberately slow (tens of ms of CPU): keep it off the event loop.
        if not await anyio.to_thread.run_sync(verifier.verify, plaintext, client.key_hash):
            return None
        # Detach both rows so a later rollback in this session (idempotency release, failed
        # checkout) cannot expire them; async sessions cannot lazy-reload attributes.
        self.session.expunge(client)
        self.session.expunge(merchant)
        return AgentPrincipal(client=client, merchant=merchant)


def request_fingerprint(method: str, path: str, body: Any) -> str:
    """Hash a request's method, path and canonical JSON body to detect Idempotency-Key reuse."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{method} {path}\n{canonical}".encode()).hexdigest()


class IdempotencyConflict(Exception):
    """An Idempotency-Key was reused for another request, or its first request is still running."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "mismatch" or "in_progress"


class IdempotencyRepository:
    """Stores the outcome of each agent request per Idempotency-Key so retries replay it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def begin(
        self, client_id: uuid.UUID, key: str, request_hash: str
    ) -> IdempotencyRecord | None:
        """Claim the key. Returns the stored record to replay, or None when the caller should run.

        Raises IdempotencyConflict when the key was used for a different request, or the first
        request with this key is still running.
        """
        existing = await self._get(client_id, key)
        if existing is not None and existing.created_at < utcnow() - IDEMPOTENCY_TTL:
            await self.session.delete(existing)
            await self.session.commit()
            existing = None
        if existing is None:
            self.session.add(
                IdempotencyRecord(agent_client_id=client_id, key=key, request_hash=request_hash)
            )
            try:
                await self.session.commit()
                return None
            except IntegrityError:
                await self.session.rollback()
                existing = await self._get(client_id, key)
                if existing is None:
                    raise IdempotencyConflict("in_progress") from None
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("mismatch")
        if existing.status_code is None:
            raise IdempotencyConflict("in_progress")
        return existing

    async def finish(
        self, client_id: uuid.UUID, key: str, status: int, body: dict[str, Any]
    ) -> None:
        """Store the response for a key so later retries replay it."""
        record = await self._get(client_id, key)
        if record is not None:
            record.status_code = status
            record.response_body = body
            self.session.add(record)
            await self.session.commit()

    async def release(self, client_id: uuid.UUID, key: str) -> None:
        """Forget a claim whose request failed unexpectedly, so the agent can retry."""
        await self.session.rollback()
        record = await self._get(client_id, key)
        if record is not None and record.status_code is None:
            await self.session.delete(record)
            await self.session.commit()

    async def _get(self, client_id: uuid.UUID, key: str) -> IdempotencyRecord | None:
        return (
            await self.session.exec(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.agent_client_id == client_id, IdempotencyRecord.key == key
                )
            )
        ).first()
