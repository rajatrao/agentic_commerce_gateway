"""Merchant view of agent orders (the transaction log) and payment settings."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col, select

from app.api.deps import CurrentMerchant, SessionDep, SettingsDep, require_service_scopes
from app.models import AgentClient
from app.repositories.transactions import TransactionRepository
from app.schemas.agent import PaymentSettings, PaymentSettingsOut, TransactionOut, TransactionPage

router = APIRouter(prefix="/merchant", tags=["agent-orders"])
write = [Depends(require_service_scopes("merchant:write"))]


@router.get("/transactions", response_model=TransactionPage)
async def list_transactions(
    merchant: CurrentMerchant,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> TransactionPage:
    total, rows = await TransactionRepository(session).list_for(
        merchant.id, limit=limit, offset=offset
    )
    client_ids = {t.agent_client_id for t in rows if t.agent_client_id}
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
    store = merchant.shop_domain.removesuffix(".myshopify.com")
    return TransactionPage(
        total=total,
        items=[
            TransactionOut(
                id=t.id,
                protocol=t.protocol.value,
                agent_name=names.get(t.agent_client_id) if t.agent_client_id else None,
                status=t.status.value,
                amount=t.amount,
                currency=t.currency,
                payment_ref=t.payment_ref,
                order_gid=t.order_gid,
                order_name=t.order_name,
                admin_url=_admin_url(store, t.order_gid),
                error_code=t.error_code,
                error_detail=t.error_detail,
                created_at=t.created_at,
            )
            for t in rows
        ],
    )


def _admin_url(store: str, order_gid: str | None) -> str | None:
    if not order_gid:
        return None
    return f"https://admin.shopify.com/store/{store}/orders/{order_gid.rsplit('/', 1)[-1]}"


def _payment_settings(merchant: CurrentMerchant, settings: SettingsDep) -> PaymentSettingsOut:
    key = settings.stripe_secret_key
    return PaymentSettingsOut(
        stripe_account_id=merchant.settings.get("stripe_account_id"),
        stripe_configured=bool(key),
        test_mode=key.startswith(("sk_test_", "rk_test_")),
    )


@router.get("/payments", response_model=PaymentSettingsOut)
async def get_payment_settings(
    merchant: CurrentMerchant, settings: SettingsDep
) -> PaymentSettingsOut:
    return _payment_settings(merchant, settings)


@router.put("/payments", response_model=PaymentSettingsOut, dependencies=write)
async def put_payment_settings(
    body: PaymentSettings, merchant: CurrentMerchant, session: SessionDep, settings: SettingsDep
) -> PaymentSettingsOut:
    merchant.settings = {**merchant.settings, "stripe_account_id": body.stripe_account_id}
    flag_modified(merchant, "settings")
    session.add(merchant)
    await session.commit()
    return _payment_settings(merchant, settings)
