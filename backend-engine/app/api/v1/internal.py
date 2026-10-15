"""Service-to-service routes, called only by frontend-ui."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import SessionDep, SettingsDep, require_service_scopes
from app.core.security import ServiceClaims, TokenCipher
from app.repositories.merchants import MerchantRepository
from app.schemas.merchant import MerchantOut, MerchantSyncRequest

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/merchants/sync", response_model=MerchantOut)
def sync_merchant(
    body: MerchantSyncRequest,
    session: SessionDep,
    settings: SettingsDep,
    claims: Annotated[ServiceClaims, Depends(require_service_scopes("internal:sync"))],
) -> MerchantOut:
    """Called from the Shopify `afterAuth` hook to hand over the offline Admin API token."""
    if claims.shop != body.shop:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "token subject does not match shop")
    token_enc = TokenCipher(settings.token_encryption_key).encrypt(body.access_token)
    merchant = MerchantRepository(session).upsert_installation(body.shop, token_enc, body.scopes)
    return MerchantOut(
        id=merchant.id,
        shop_domain=merchant.shop_domain,
        status=merchant.status.value,
        installed_at=merchant.installed_at,
    )
