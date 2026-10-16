from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import WebhookEvent
from app.models.base import utcnow


class WebhookEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_if_new(
        self, webhook_id: str, shop_domain: str, topic: str, payload: dict[str, Any]
    ) -> bool:
        """Insert the event; return False if this webhook id was already received."""
        stmt = (
            insert(WebhookEvent)
            .values(
                webhook_id=webhook_id,
                shop_domain=shop_domain,
                topic=topic,
                payload=payload,
                received_at=utcnow(),
            )
            .on_conflict_do_nothing(index_elements=["webhook_id"])
            .returning(WebhookEvent.__table__.c.id)  # type: ignore[attr-defined]
        )
        inserted = (await self.session.execute(stmt)).first()
        await self.session.commit()
        return inserted is not None

    async def mark_processed(self, webhook_id: str) -> None:
        await self.session.exec(
            update(WebhookEvent)
            .where(col(WebhookEvent.webhook_id) == webhook_id)
            .values(processed_at=utcnow())
        )
        await self.session.commit()
