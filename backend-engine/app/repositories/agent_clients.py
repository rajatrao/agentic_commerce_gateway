import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.models import AgentClient, AgentProtocol, IdempotencyRecord, Merchant, MerchantStatus
from app.models.base import utcnow
from app.services.agent_keys import IssuedKey, KeyVerifier, key_prefix

IDEMPOTENCY_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class AgentPrincipal:
    client: AgentClient
    merchant: Merchant


class AgentClientRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for(self, merchant_id: uuid.UUID) -> Sequence[AgentClient]:
        return self.session.exec(
            select(AgentClient)
            .where(AgentClient.merchant_id == merchant_id)
            .order_by(col(AgentClient.created_at).desc())
        ).all()

    def count_active(self, merchant_id: uuid.UUID) -> int:
        return len([c for c in self.list_for(merchant_id) if c.revoked_at is None])

    def create(
        self,
        merchant_id: uuid.UUID,
        name: str,
        protocol: AgentProtocol,
        scopes: list[str],
        key: IssuedKey,
    ) -> AgentClient:
        client = AgentClient(
            merchant_id=merchant_id,
            name=name,
            protocol=protocol,
            scopes=scopes,
            key_prefix=key.prefix,
            key_hash=key.hash,
        )
        self.session.add(client)
        self.session.commit()
        self.session.refresh(client)
        return client

    def revoke(self, merchant_id: uuid.UUID, client_id: uuid.UUID) -> AgentClient | None:
        client = self.session.get(AgentClient, client_id)
        if client is None or client.merchant_id != merchant_id:
            return None
        if client.revoked_at is None:
            client.revoked_at = utcnow()
            self.session.add(client)
            self.session.commit()
        return client

    def authenticate(self, plaintext: str, verifier: KeyVerifier) -> AgentPrincipal | None:
        """Resolve an agent API key to its active client and installed merchant."""
        prefix = key_prefix(plaintext)
        if prefix is None:
            return None
        row = self.session.exec(
            select(AgentClient, Merchant)
            .join(Merchant, col(Merchant.id) == col(AgentClient.merchant_id))
            .where(AgentClient.key_prefix == prefix)
        ).first()
        if row is None:
            return None
        client, merchant = row
        if client.revoked_at is not None or merchant.status is not MerchantStatus.ACTIVE:
            return None
        if not verifier.verify(plaintext, client.key_hash):
            return None
        return AgentPrincipal(client=client, merchant=merchant)


def request_fingerprint(method: str, path: str, body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{method} {path}\n{canonical}".encode()).hexdigest()


class IdempotencyConflict(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "mismatch" or "in_progress"


class IdempotencyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def begin(self, client_id: uuid.UUID, key: str, request_hash: str) -> IdempotencyRecord | None:
        """Claim the key. Returns the stored record to replay, or None when the caller should run.

        Raises IdempotencyConflict when the key was used for a different request, or the first
        request with this key is still running.
        """
        existing = self._get(client_id, key)
        if existing is not None and existing.created_at < utcnow() - IDEMPOTENCY_TTL:
            self.session.delete(existing)
            self.session.commit()
            existing = None
        if existing is None:
            self.session.add(
                IdempotencyRecord(agent_client_id=client_id, key=key, request_hash=request_hash)
            )
            try:
                self.session.commit()
                return None
            except IntegrityError:
                self.session.rollback()
                existing = self._get(client_id, key)
                if existing is None:
                    raise IdempotencyConflict("in_progress") from None
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("mismatch")
        if existing.status_code is None:
            raise IdempotencyConflict("in_progress")
        return existing

    def finish(self, client_id: uuid.UUID, key: str, status: int, body: dict[str, Any]) -> None:
        record = self._get(client_id, key)
        if record is not None:
            record.status_code = status
            record.response_body = body
            self.session.add(record)
            self.session.commit()

    def release(self, client_id: uuid.UUID, key: str) -> None:
        """Forget a claim whose request failed unexpectedly, so the agent can retry."""
        self.session.rollback()
        record = self._get(client_id, key)
        if record is not None and record.status_code is None:
            self.session.delete(record)
            self.session.commit()

    def _get(self, client_id: uuid.UUID, key: str) -> IdempotencyRecord | None:
        return self.session.exec(
            select(IdempotencyRecord).where(
                IdempotencyRecord.agent_client_id == client_id, IdempotencyRecord.key == key
            )
        ).first()
