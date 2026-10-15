from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session

from app.models import WebhookEvent
from app.models.base import utcnow


class WebhookEventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record_if_new(
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
        inserted = self.session.execute(stmt).first()
        self.session.commit()
        return inserted is not None

    def mark_processed(self, webhook_id: str) -> None:
        event = self.session.query(WebhookEvent).filter_by(webhook_id=webhook_id).one_or_none()
        if event is not None:
            event.processed_at = utcnow()
            self.session.add(event)
            self.session.commit()
