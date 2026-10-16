"""FastAPI dependencies for authentication and shared resources."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

import httpx
from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.security import AuthError, ServiceClaims, verify_service_jwt
from app.models import Merchant
from app.repositories.merchants import MerchantRepository
from app.repositories.shared_state import ReplayGuard
from app.services.shopify_client import ShopifyTokenProvider

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return authorization.split(" ", 1)[1].strip()


async def service_claims(
    settings: SettingsDep,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> ServiceClaims:
    """Verify the frontend-ui service JWT once per request (FastAPI caches this dependency)."""
    try:
        claims = verify_service_jwt(_bearer(authorization), settings)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    # The replay guard lives in Postgres so a token is accepted once across all replicas.
    if not await ReplayGuard(session).claim(claims.jti, claims.expires_at):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token replayed")
    return claims


def require_service_scopes(*required: str) -> Callable[..., Awaitable[ServiceClaims]]:
    """Require the given scopes on the request's service JWT."""

    async def dependency(
        claims: Annotated[ServiceClaims, Depends(service_claims)],
    ) -> ServiceClaims:
        missing = set(required) - claims.scopes
        if missing:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing scopes: {sorted(missing)}")
        return claims

    return dependency


async def current_merchant(
    session: SessionDep,
    claims: Annotated[ServiceClaims, Depends(require_service_scopes("merchant:read"))],
) -> Merchant:
    merchant = await MerchantRepository(session).get_active_by_shop(claims.shop)
    if merchant is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "merchant not installed")
    return merchant


CurrentMerchant = Annotated[Merchant, Depends(current_merchant)]


async def get_http_client(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    yield request.app.state.http_client


def get_token_provider(request: Request) -> ShopifyTokenProvider:
    return request.app.state.shopify_tokens  # type: ignore[no-any-return]


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
TokenProviderDep = Annotated[ShopifyTokenProvider, Depends(get_token_provider)]
