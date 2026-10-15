"""Shared plumbing for agent-facing protocol routes (UCP, ACP): API-key auth, idempotency and
protocol-shaped errors. Routers stay thin: parse with an adapter, call `CartManager` or
`PaymentProcessor`, render."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.core.config import Settings
from app.jobs.catalog_audit import shopify_client_for
from app.models import AgentCart, AgentProtocol, Merchant
from app.protocols.base import PaymentPayload, ProtocolAdapter, ProtocolError
from app.repositories.agent_clients import (
    AgentClientRepository,
    AgentPrincipal,
    IdempotencyConflict,
    IdempotencyRepository,
    request_fingerprint,
)
from app.repositories.carts import CartRepository
from app.repositories.transactions import TransactionRepository
from app.services.agent_keys import KeyVerifier
from app.services.cart_manager import CartError, CartManager, ShopifyQuery
from app.services.payment_gateway import PaymentGateway, StripeGateway
from app.services.payment_processor import (
    CheckoutError,
    CheckoutOutcome,
    PaymentProcessor,
    PaymentRequest,
)
from app.services.shopify_client import ShopifyAuthError, ShopifyError, ShopifyTokenProvider

log = structlog.get_logger(__name__)

CHECKOUT_SCOPE = "checkout"
key_verifier = KeyVerifier()


def presented_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return request.headers.get("x-api-key")


def resolve_agent(
    session: Session, key: str | None, protocol: AgentProtocol | None, scope: str | None
) -> AgentPrincipal:
    if not key:
        raise ProtocolError(401, "unauthorized", "missing API key")
    principal = AgentClientRepository(session).authenticate(key, key_verifier)
    if principal is None:
        raise ProtocolError(401, "unauthorized", "invalid or revoked API key")
    if protocol is not None and principal.client.protocol is not protocol:
        raise ProtocolError(
            403, "forbidden", f"this key is for {principal.client.protocol.value.upper()}"
        )
    if scope and scope not in principal.client.scopes:
        raise ProtocolError(403, "forbidden", f"this key lacks the {scope!r} scope")
    return principal


@dataclass(frozen=True)
class Rendered:
    """An operation result that is already in wire format."""

    status: int
    body: dict[str, Any]


def payment_gateway(state: Any, settings: Settings) -> PaymentGateway:
    """One Stripe client per process, created on first use (tests swap `state.payment_gateway`)."""
    gateway: PaymentGateway | None = getattr(state, "payment_gateway", None)
    if gateway is None:
        gateway = StripeGateway(settings.stripe_secret_key, settings.stripe_api_version)
        state.payment_gateway = gateway
    return gateway


def payment_processor(
    session: Session,
    merchant: Merchant,
    carts: CartManager,
    shopify: ShopifyQuery,
    state: Any,
    settings: Settings,
) -> PaymentProcessor:
    return PaymentProcessor(
        carts,
        TransactionRepository(session),
        shopify,
        lambda: payment_gateway(state, settings),
        stripe_account=merchant.settings.get("stripe_account_id") or None,
        reservation_minutes=settings.checkout_reservation_minutes,
    )


CHECKOUT_ERROR_STATUS = {
    "not_found": 404,
    "in_progress": 409,
    "already_completed": 409,
    "not_modifiable": 405,
    "payments_not_configured": 503,
}


class CheckoutHandler:
    def __init__(
        self,
        adapter: ProtocolAdapter,
        request: Request,
        session: Session,
        settings: Settings,
        tokens: ShopifyTokenProvider,
        *,
        any_protocol: bool = False,
    ) -> None:
        self.adapter = adapter
        self.request = request
        self.session = session
        self.principal = resolve_agent(
            session,
            presented_key(request),
            None if any_protocol else adapter.protocol,
            CHECKOUT_SCOPE,
        )
        client = shopify_client_for(
            self.principal.merchant, request.app.state.http_client, tokens, settings
        )
        self.carts = CartManager(CartRepository(session), client)
        self.payments = payment_processor(
            session, self.principal.merchant, self.carts, client, request.app.state, settings
        )

    async def checkout(self, cart_id: str, payment: PaymentPayload) -> CheckoutOutcome:
        try:
            parsed = uuid.UUID(cart_id)
        except ValueError:
            raise ProtocolError(404, "not_found", "checkout session not found") from None
        return await self.payments.checkout(
            self.merchant_id,
            parsed,
            PaymentRequest(
                token=payment.token,
                provider=payment.provider,
                buyer=payment.buyer,
                idempotency_key=self.request.headers.get("idempotency-key") or "",
                agent_client_id=self.principal.client.id,
            ),
        )

    @property
    def merchant_id(self) -> uuid.UUID:
        return self.principal.merchant.id

    def cart(self, cart_id: str) -> AgentCart:
        try:
            parsed = uuid.UUID(cart_id)
        except ValueError:
            raise ProtocolError(404, "not_found", "checkout session not found") from None
        return self._cart_call(lambda: self.carts.get(self.merchant_id, parsed))

    async def body(self) -> Any:
        try:
            return await self.request.json()
        except (JSONDecodeError, UnicodeDecodeError):
            raise ProtocolError(400, "invalid", "request body must be JSON") from None

    def respond(self, cart: AgentCart, status: int = 200) -> JSONResponse:
        return JSONResponse(self.adapter.render(cart), status, headers=self._echo_headers())

    async def mutate(
        self,
        body: Any,
        operation: Callable[[], Awaitable[AgentCart | Rendered]],
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
            stored = records.begin(client_id, key, fingerprint)
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
                records.release(client_id, key)
            else:
                records.finish(client_id, key, exc.status, self.adapter.render_error(exc))
            raise
        except Exception:
            records.release(client_id, key)
            raise
        if isinstance(result, Rendered):
            status, rendered = result.status, result.body
        else:
            rendered = self.adapter.render(result)
        records.finish(client_id, key, status, rendered)
        return JSONResponse(rendered, status, headers=self._echo_headers())

    async def _upstream_call(
        self, operation: Callable[[], Awaitable[AgentCart | Rendered]]
    ) -> AgentCart | Rendered:
        try:
            return await operation()
        except CheckoutError as exc:
            status = CHECKOUT_ERROR_STATUS.get(exc.code, 400)
            raise ProtocolError(status, exc.code, exc.message) from exc
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
    def _cart_call(fn: Callable[[], AgentCart]) -> AgentCart:
        try:
            return fn()
        except CartError as exc:
            raise ProtocolError.from_cart_error(exc) from exc

    def cancel(self, cart: AgentCart) -> AgentCart:
        return self._cart_call(lambda: self.carts.cancel(cart))

    def _echo_headers(self) -> dict[str, str]:
        return {
            name: value
            for name in ("Idempotency-Key", "Request-Id")
            if (value := self.request.headers.get(name))
        }


def protocol_error_response(
    adapter: ProtocolAdapter, request: Request, exc: ProtocolError
) -> JSONResponse:
    headers = {n: v for n in ("Idempotency-Key", "Request-Id") if (v := request.headers.get(n))}
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(adapter.render_error(exc), exc.status, headers=headers)
