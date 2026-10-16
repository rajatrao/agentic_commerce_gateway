import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    AgentCart,
    AgentTransaction,
    CartStatus,
    TransactionEvent,
    TransactionStatus,
)
from app.models.base import utcnow

# A cart with one of these is being paid, or has been.
BLOCKING = (
    TransactionStatus.PENDING,
    TransactionStatus.AUTHORIZED,
    TransactionStatus.CAPTURED,
    TransactionStatus.ORDER_CREATED,
)


class TransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def by_idempotency_key(self, merchant_id: uuid.UUID, key: str) -> AgentTransaction | None:
        return (
            await self.session.exec(
                select(AgentTransaction).where(
                    AgentTransaction.merchant_id == merchant_id,
                    AgentTransaction.idempotency_key == key,
                )
            )
        ).first()

    async def lock_cart(self, merchant_id: uuid.UUID, cart_id: uuid.UUID) -> AgentCart | None:
        """Row-lock the cart until the next commit so two checkouts cannot both start."""
        return (
            await self.session.exec(
                select(AgentCart)
                .where(AgentCart.id == cart_id, AgentCart.merchant_id == merchant_id)
                .with_for_update()
            )
        ).first()

    async def blocking_for_cart(self, cart_id: uuid.UUID) -> AgentTransaction | None:
        return (
            await self.session.exec(
                select(AgentTransaction)
                .where(
                    AgentTransaction.cart_id == cart_id,
                    col(AgentTransaction.status).in_(BLOCKING),
                )
                .order_by(col(AgentTransaction.created_at).desc())
            )
        ).first()

    async def completed_for_cart(self, cart: AgentCart) -> AgentTransaction | None:
        if cart.status is not CartStatus.COMPLETED:
            return None
        return (
            await self.session.exec(
                select(AgentTransaction).where(
                    AgentTransaction.cart_id == cart.id,
                    AgentTransaction.status == TransactionStatus.ORDER_CREATED,
                )
            )
        ).first()

    async def save(
        self, txn: AgentTransaction, event: str | None = None, **payload: Any
    ) -> AgentTransaction:
        """Persist the transaction, optionally appending an audit event, in one commit."""
        txn.updated_at = utcnow()
        self.session.add(txn)
        if event:
            await self.session.flush()
            self.session.add(
                TransactionEvent(transaction_id=txn.id, event_type=event, payload=payload)
            )
        await self.session.commit()
        await self.session.refresh(txn)
        return txn

    async def events(self, transaction_id: uuid.UUID) -> Sequence[TransactionEvent]:
        return (
            await self.session.exec(
                select(TransactionEvent)
                .where(TransactionEvent.transaction_id == transaction_id)
                .order_by(col(TransactionEvent.id))
            )
        ).all()

    async def list_for(
        self, merchant_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[int, Sequence[AgentTransaction]]:
        where = AgentTransaction.merchant_id == merchant_id
        total = (
            await self.session.exec(select(func.count()).select_from(AgentTransaction).where(where))
        ).one()
        rows = (
            await self.session.exec(
                select(AgentTransaction)
                .where(where)
                .order_by(col(AgentTransaction.created_at).desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return total, rows
