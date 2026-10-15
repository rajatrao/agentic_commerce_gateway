from sqlmodel import Session, select

from app.models import Merchant, MerchantStatus
from app.models.base import utcnow


class MerchantRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_shop(self, shop_domain: str) -> Merchant | None:
        return self.session.exec(
            select(Merchant).where(Merchant.shop_domain == shop_domain)
        ).first()

    def get_active_by_shop(self, shop_domain: str) -> Merchant | None:
        merchant = self.get_by_shop(shop_domain)
        if merchant is None or merchant.status is not MerchantStatus.ACTIVE:
            return None
        return merchant

    def upsert_installation(self, shop_domain: str, token_enc: bytes, scopes: str) -> Merchant:
        merchant = self.get_by_shop(shop_domain)
        if merchant is None:
            merchant = Merchant(shop_domain=shop_domain)
        elif merchant.status is MerchantStatus.UNINSTALLED:
            merchant.installed_at = utcnow()
        merchant.access_token_enc = token_enc
        merchant.scopes = scopes
        merchant.status = MerchantStatus.ACTIVE
        merchant.uninstalled_at = None
        self.session.add(merchant)
        self.session.commit()
        self.session.refresh(merchant)
        return merchant

    def mark_uninstalled(self, shop_domain: str) -> Merchant | None:
        merchant = self.get_by_shop(shop_domain)
        if merchant is None:
            return None
        merchant.status = MerchantStatus.UNINSTALLED
        merchant.uninstalled_at = utcnow()
        merchant.access_token_enc = None
        self.session.add(merchant)
        self.session.commit()
        return merchant
