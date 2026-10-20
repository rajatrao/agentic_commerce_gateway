import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, case, or_, update
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import AgentCart, CartStatus
from app.models.base import utcnow


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

    async def count_ordered(self, merchant_id: uuid.UUID) -> int:
        return int(
            (
                await self.session.exec(
                    select(func.count())
                    .select_from(AgentCart)
                    .where(
                        AgentCart.merchant_id == merchant_id,
                        col(AgentCart.order_gid).is_not(None),
                    )
                )
            ).one()
        )

    async def link_order(
        self,
        merchant_id: uuid.UUID,
        cart_id: uuid.UUID,
        *,
        order_gid: str,
        order_name: str | None,
        order_status_url: str | None,
    ) -> bool:
        """Mark the cart ordered. Only the first order counts if a checkout link is reused."""
        now = utcnow()
        result = await self.session.exec(
            update(AgentCart)
            .where(
                col(AgentCart.id) == cart_id,
                col(AgentCart.merchant_id) == merchant_id,
                col(AgentCart.order_gid).is_(None),
            )
            .values(
                status=CartStatus.COMPLETED,
                order_gid=order_gid,
                order_name=order_name,
                order_status_url=order_status_url,
                ordered_at=now,
                checkout_url=None,
                messages=[],
                updated_at=now,
            )
        )
        await self.session.commit()
        return bool(result.rowcount)

    async def redact_buyer(
        self, merchant_id: uuid.UUID, *, email: str | None, order_gids: Iterable[str]
    ) -> int:
        """customers/redact: drop buyer details from carts for that customer or their orders."""
        conditions: list[ColumnElement[bool]] = []
        if gids := list(order_gids):
            conditions.append(col(AgentCart.order_gid).in_(gids))
        if email:
            conditions.append(func.lower(col(AgentCart.buyer)["email"].astext) == email.lower())
        if not conditions:
            return 0
        result = await self.session.exec(
            update(AgentCart)
            .where(col(AgentCart.merchant_id) == merchant_id, or_(*conditions))
            .values(
                buyer=None,
                shipping_address=None,
                order_status_url=None,
                # The checkout link embeds the buyer's details, so live carts are closed too.
                checkout_url=None,
                status=case(
                    (
                        col(AgentCart.status).in_([CartStatus.OPEN, CartStatus.READY]),
                        CartStatus.CANCELED,
                    ),
                    else_=col(AgentCart.status),
                ),
                updated_at=utcnow(),
            )
        )
        await self.session.commit()
        return int(result.rowcount)
