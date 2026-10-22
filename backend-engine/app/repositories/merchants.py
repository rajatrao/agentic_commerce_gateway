"""Merchant install records."""

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import Merchant, MerchantStatus
from app.models.base import utcnow


class MerchantRepository:
    """Reads and updates merchant install records."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_shop(self, shop_domain: str) -> Merchant | None:
        """Return the merchant for a shop domain, installed or not."""
        return (
            await self.session.exec(select(Merchant).where(Merchant.shop_domain == shop_domain))
        ).first()

    async def get_active_by_shop(self, shop_domain: str) -> Merchant | None:
        """Return the merchant for a shop domain only if the app is installed."""
        merchant = await self.get_by_shop(shop_domain)
        if merchant is None or merchant.status is not MerchantStatus.ACTIVE:
            return None
        return merchant

    async def upsert_installation(
        self, shop_domain: str, token_enc: bytes, scopes: str
    ) -> Merchant:
        """Record an install or reinstall with the shop's encrypted offline token and scopes."""
        merchant = await self.get_by_shop(shop_domain)
        if merchant is None:
            merchant = Merchant(shop_domain=shop_domain)
        elif merchant.status is MerchantStatus.UNINSTALLED:
            merchant.installed_at = utcnow()
        merchant.access_token_enc = token_enc
        merchant.scopes = scopes
        merchant.status = MerchantStatus.ACTIVE
        merchant.uninstalled_at = None
        self.session.add(merchant)
        await self.session.commit()
        await self.session.refresh(merchant)
        return merchant

    async def mark_uninstalled(self, shop_domain: str) -> Merchant | None:
        """Mark the shop uninstalled and drop its access token."""
        merchant = await self.get_by_shop(shop_domain)
        if merchant is None:
            return None
        merchant.status = MerchantStatus.UNINSTALLED
        merchant.uninstalled_at = utcnow()
        merchant.access_token_enc = None
        self.session.add(merchant)
        await self.session.commit()
        return merchant
