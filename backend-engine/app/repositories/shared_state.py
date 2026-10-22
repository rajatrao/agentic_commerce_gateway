"""Postgres-backed state every replica shares: the service-JWT replay guard and llm.txt cache."""

import random
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import LlmTxtCacheEntry, ServiceTokenJti
from app.models.base import utcnow

# Expired jti rows are swept on roughly one in this many inserts.
PURGE_ONE_IN = 100


class ReplayGuard:
    """Accepts each service-JWT `jti` once across all replicas, until the token expires."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(self, jti: str, expires_at: datetime) -> bool:
        """Record the jti; False if any replica has already seen it."""
        if random.randrange(PURGE_ONE_IN) == 0:  # noqa: S311 - not security sensitive
            await self.session.exec(
                delete(ServiceTokenJti).where(col(ServiceTokenJti.expires_at) < utcnow())
            )
        inserted = (
            await self.session.exec(
                insert(ServiceTokenJti)
                .values(jti=jti, expires_at=expires_at)
                .on_conflict_do_nothing(index_elements=["jti"])
                .returning(col(ServiceTokenJti.jti))
            )
        ).first()
        await self.session.commit()
        return inserted is not None


class LlmTxtCacheRepository:
    """Rendered llm.txt per shop. Invalidation (e.g. an FAQ edit) reaches every replica."""

    def __init__(self, session: AsyncSession, ttl_seconds: int) -> None:
        self.session = session
        self.ttl = timedelta(seconds=ttl_seconds)

    async def get(self, shop: str) -> str | None:
        """Return the cached `llm.txt` for a shop if it is still fresh."""
        entry = await self.session.get(LlmTxtCacheEntry, shop)
        if entry is None or entry.generated_at <= utcnow() - self.ttl:
            return None
        return entry.body

    async def put(self, shop: str, body: str) -> None:
        """Store a freshly rendered `llm.txt` for a shop."""
        now = utcnow()
        await self.session.exec(
            insert(LlmTxtCacheEntry)
            .values(shop_domain=shop, body=body, generated_at=now)
            .on_conflict_do_update(
                index_elements=["shop_domain"], set_={"body": body, "generated_at": now}
            )
        )
        await self.session.commit()

    async def invalidate(self, shop: str) -> None:
        """Drop a shop's cached `llm.txt` so the next request re-renders it."""
        await self.session.exec(
            delete(LlmTxtCacheEntry).where(col(LlmTxtCacheEntry.shop_domain) == shop)
        )
        await self.session.commit()
