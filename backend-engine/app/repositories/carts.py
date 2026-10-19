import uuid

from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import AgentCart


class CartRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, merchant_id: uuid.UUID, cart_id: uuid.UUID) -> AgentCart | None:
        return (
            await self.session.exec(
                select(AgentCart).where(
                    AgentCart.id == cart_id, AgentCart.merchant_id == merchant_id
                )
            )
        ).first()

    async def save(self, cart: AgentCart) -> AgentCart:
        self.session.add(cart)
        await self.session.commit()
        await self.session.refresh(cart)
        return cart

    async def list_for(
        self, merchant_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[int, list[AgentCart]]:
        total = (
            await self.session.exec(
                select(func.count())
                .select_from(AgentCart)
                .where(AgentCart.merchant_id == merchant_id)
            )
        ).one()
        rows = (
            await self.session.exec(
                select(AgentCart)
                .where(AgentCart.merchant_id == merchant_id)
                .order_by(col(AgentCart.created_at).desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return int(total), list(rows)
