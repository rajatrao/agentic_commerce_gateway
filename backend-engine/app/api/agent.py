"""Shared plumbing for agent-facing protocol routes (UCP, ACP): API-key auth, idempotency and
protocol-shaped errors. Routers stay thin: parse with an adapter, call `CartManager`, render.
Buyers always pay on the merchant's own Shopify checkout, so nothing here takes payments."""

import uuid
from collections.abc import Awaitable, Callable
from json import JSONDecodeError
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.jobs.catalog_audit import shopify_client_for
from app.models import AgentCart, AgentProtocol
from app.protocols.base import ProtocolAdapter, ProtocolError
from app.repositories.agent_clients import (
    AgentClientRepository,
    AgentPrincipal,
    IdempotencyConflict,
    IdempotencyRepository,
    request_fingerprint,
)
from app.repositories.carts import CartRepository
from app.services.agent_keys import KeyVerifier
from app.services.cart_manager import CartError, CartManager
from app.services.shopify_client import ShopifyAuthError, ShopifyError, ShopifyTokenProvider

log = structlog.get_logger(__name__)

CHECKOUT_SCOPE = "checkout"
key_verifier = KeyVerifier()


def presented_key(request: Request) -> str | None:
    """Read the agent API key from `Authorization: Bearer` or `X-API-Key`."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return request.headers.get("x-api-key")


async def resolve_agent(
    session: AsyncSession, key: str | None, protocol: AgentProtocol | None, scope: str | None
) -> AgentPrincipal:
    """Authenticate an agent key and check it belongs to `protocol` and holds `scope`, raising
    `ProtocolError` otherwise."""
    if not key:
        raise ProtocolError(401, "unauthorized", "missing API key")
    principal = await AgentClientRepository(session).authenticate(key, key_verifier)
    if principal is None:
        raise ProtocolError(401, "unauthorized", "invalid or revoked API key")
    if protocol is not None and principal.client.protocol is not protocol:
        raise ProtocolError(
            403, "forbidden", f"this key is for {principal.client.protocol.value.upper()}"
        )
    if scope and scope not in principal.client.scopes:
        raise ProtocolError(403, "forbidden", f"this key lacks the {scope!r} scope")
    return principal


class CheckoutHandler:
    """Per-request helper for checkout routes: loads carts for the calling agent, enforces
    idempotency and maps errors to protocol responses."""

    def __init__(
        self,
        adapter: ProtocolAdapter,
        request: Request,
        session: AsyncSession,
        settings: Settings,
        tokens: ShopifyTokenProvider,
        principal: AgentPrincipal,
    ) -> None:
        self.adapter = adapter
        self.request = request
        self.session = session
        self.principal = principal
        client = shopify_client_for(
            self.principal.merchant, request.app.state.http_client, tokens, settings
        )
        self.carts = CartManager(
            CartRepository(session), client, self.principal.merchant.shop_domain
        )

    @classmethod
    async def create(
        cls,
        adapter: ProtocolAdapter,
        request: Request,
        session: AsyncSession,
        settings: Settings,
        tokens: ShopifyTokenProvider,
        *,
        any_protocol: bool = False,
    ) -> "CheckoutHandler":
        """Authenticate the request's agent key for checkout and build a handler for it."""
        principal = await resolve_agent(
            session,
            presented_key(request),
            None if any_protocol else adapter.protocol,
            CHECKOUT_SCOPE,
        )
        return cls(adapter, request, session, settings, tokens, principal)

    @property
    def merchant_id(self) -> uuid.UUID:
        """The id of the merchant the agent key belongs to."""
        return self.principal.merchant.id

    async def cart(self, cart_id: str) -> AgentCart:
        """Load one of this merchant's carts by id, or raise a 404 `ProtocolError`."""
        try:
            parsed = uuid.UUID(cart_id)
        except ValueError:
            raise ProtocolError(404, "not_found", "checkout session not found") from None
        return await self._cart_call(self.carts.get(self.merchant_id, parsed))

    async def body(self) -> Any:
        """Parse the request body as JSON, or raise a 400 `ProtocolError`."""
        try:
            return await self.request.json()
        except (JSONDecodeError, UnicodeDecodeError):
            raise ProtocolError(400, "invalid", "request body must be JSON") from None

    def respond(self, cart: AgentCart, status: int = 200) -> JSONResponse:
        """Render a cart in the adapter's wire format, echoing the request's idempotency and request
        ids."""
        return JSONResponse(self.adapter.render(cart), status, headers=self._echo_headers())

    async def mutate(
        self,
        body: Any,
        operation: Callable[[], Awaitable[AgentCart]],
        status: int = 200,
    ) -> JSONResponse:
        """Run a state-changing operation once per Idempotency-Key, replaying stored results."""
        key = self.request.headers.get("idempotency-key")
        if not key or len(key) > 255:
            raise ProtocolError(400, "invalid", "Idempotency-Key header is required")
        client_id = self.principal.client.id
        records = IdempotencyRepository(self.session)
        fingerprint = request_fingerprint(self.request.method, self.request.url.path, body)
        try:
            stored = await records.begin(client_id, key, fingerprint)
        except IdempotencyConflict as exc:
            if exc.reason == "mismatch":
                raise ProtocolError(
                    409,
                    "request_not_idempotent",
                    "Idempotency-Key was already used with a different request",
                ) from None
            raise ProtocolError(
                409, "idempotency_in_progress", "a request with this key is in progress"
            ) from None
        if stored is not None:
            return JSONResponse(
                stored.response_body,
                stored.status_code or 200,
                headers={**self._echo_headers(), "Idempotent-Replayed": "true"},
            )

        try:
            result = await self._upstream_call(operation)
        except ProtocolError as exc:
            if exc.status >= 500:
                await records.release(client_id, key)
            else:
                await records.finish(client_id, key, exc.status, self.adapter.render_error(exc))
            raise
        except Exception:
            await records.release(client_id, key)
            raise
        rendered = self.adapter.render(result)
        await records.finish(client_id, key, status, rendered)
        return JSONResponse(rendered, status, headers=self._echo_headers())

    async def _upstream_call(self, operation: Callable[[], Awaitable[AgentCart]]) -> AgentCart:
        try:
            return await operation()
        except CartError as exc:
            raise ProtocolError.from_cart_error(exc) from exc
        except ShopifyAuthError as exc:
            log.warning(
                "agent.shopify_auth", shop=self.principal.merchant.shop_domain, error=str(exc)
            )
            raise ProtocolError(
                503, "service_unavailable", "store temporarily unavailable"
            ) from exc
        except ShopifyError as exc:
            log.warning(
                "agent.shopify_error", shop=self.principal.merchant.shop_domain, error=str(exc)
            )
            raise ProtocolError(
                502, "upstream_error", "the store could not price this cart"
            ) from exc

    @staticmethod
    async def _cart_call(call: Awaitable[AgentCart]) -> AgentCart:
        try:
            return await call
        except CartError as exc:
            raise ProtocolError.from_cart_error(exc) from exc

    async def cancel(self, cart: AgentCart) -> AgentCart:
        """Cancel a cart, mapping cart errors to protocol errors."""
        return await self._cart_call(self.carts.cancel(cart))

    def _echo_headers(self) -> dict[str, str]:
        return {
            name: value
            for name in ("Idempotency-Key", "Request-Id")
            if (value := self.request.headers.get(name))
        }


def protocol_error_response(
    adapter: ProtocolAdapter, request: Request, exc: ProtocolError
) -> JSONResponse:
    """Render a `ProtocolError` as the adapter's error body with the matching status code."""
    headers = {n: v for n in ("Idempotency-Key", "Request-Id") if (v := request.headers.get(n))}
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(adapter.render_error(exc), exc.status, headers=headers)
