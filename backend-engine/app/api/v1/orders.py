"""Merchant view of the carts agents built: what they hold and their link to Shopify checkout.

Buyers pay on the store's own Shopify checkout, so paid orders show up in Shopify's Orders list
like any other. The orders/create webhook links each one back to its cart.
"""

from fastapi import APIRouter, Query
from sqlmodel import col, select

from app.api.deps import CurrentMerchant, SessionDep
from app.models import AgentClient
from app.repositories.carts import CartRepository
from app.schemas.agent import AgentCheckoutOut, AgentCheckoutPage

router = APIRouter(prefix="/merchant", tags=["agent-checkouts"])


@router.get("/agent-checkouts", response_model=AgentCheckoutPage)
async def list_agent_checkouts(
    merchant: CurrentMerchant,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AgentCheckoutPage:
    carts = CartRepository(session)
    total, rows = await carts.list_for(merchant.id, limit=limit, offset=offset)
    admin = f"https://admin.shopify.com/store/{merchant.shop_domain.removesuffix('.myshopify.com')}"
    client_ids = {c.agent_client_id for c in rows if c.agent_client_id}
    names = (
        {
            c.id: c.name
            for c in (
                await session.exec(select(AgentClient).where(col(AgentClient.id).in_(client_ids)))
            ).all()
        }
        if client_ids
        else {}
    )
    return AgentCheckoutPage(
        total=total,
        ordered=await carts.count_ordered(merchant.id),
        items=[
            AgentCheckoutOut(
                id=c.id,
                protocol=c.protocol.value,
                agent_name=names.get(c.agent_client_id) if c.agent_client_id else None,
                status=c.status.value,
                item_count=sum(
                    li.get("quantity", 0) for li in c.lines if li.get("available", True)
                ),
                total=c.total,
                currency=c.currency,
                checkout_url=c.checkout_url,
                order_name=c.order_name,
                order_admin_url=(
                    f"{admin}/orders/{c.order_gid.rsplit('/', 1)[-1]}" if c.order_gid else None
                ),
                ordered_at=c.ordered_at,
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
            for c in rows
        ],
    )
