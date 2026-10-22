"""Agentic Commerce Protocol checkout endpoints (seller side)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.agent import CheckoutHandler
from app.api.deps import SessionDep, SettingsDep, TokenProviderDep
from app.models import AgentCart
from app.protocols.acp_adapter import AcpAdapter
from app.protocols.base import handoff_error

router = APIRouter(prefix="/acp", tags=["acp"])
adapter = AcpAdapter()


async def _handler(
    request: Request, session: SessionDep, settings: SettingsDep, tokens: TokenProviderDep
) -> CheckoutHandler:
    return await CheckoutHandler.create(adapter, request, session, settings, tokens)


@router.post("/checkout_sessions", status_code=201, summary="Create an ACP checkout session")
async def create_session(
    request: Request, session: SessionDep, settings: SettingsDep, tokens: TokenProviderDep
) -> JSONResponse:
    """Create a ACP checkout session (cart) from the agent's line items. Requires an
    Idempotency-Key."""
    h = await _handler(request, session, settings, tokens)
    body = await h.body()

    async def op() -> AgentCart:
        return await h.carts.create(
            h.merchant_id,
            adapter.protocol,
            adapter.parse_create(body),
            agent_client_id=h.principal.client.id,
            external_session_id=request.headers.get("request-id"),
        )

    return await h.mutate(body, op, status=201)


@router.get("/checkout_sessions/{session_id}", summary="Get an ACP checkout session")
async def get_session(
    session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenProviderDep,
) -> JSONResponse:
    """Return a ACP checkout session owned by the calling agent's merchant."""
    h = await _handler(request, session, settings, tokens)
    return h.respond(await h.cart(session_id))


@router.post("/checkout_sessions/{session_id}", summary="Update an ACP checkout session")
async def update_session(
    session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenProviderDep,
) -> JSONResponse:
    """Apply line, buyer or address changes to a ACP checkout session and re-price it."""
    h = await _handler(request, session, settings, tokens)
    body = await h.body()

    async def op() -> AgentCart:
        changes = adapter.parse_update(body)
        return await h.carts.update(await h.cart(session_id), changes)

    return await h.mutate(body, op)


@router.post("/checkout_sessions/{session_id}/cancel", summary="Cancel an ACP checkout session")
async def cancel_session(
    session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenProviderDep,
) -> JSONResponse:
    """Cancel a ACP checkout session."""
    h = await _handler(request, session, settings, tokens)

    async def op() -> AgentCart:
        return await h.cancel(await h.cart(session_id))

    return await h.mutate({}, op)


@router.post(
    "/checkout_sessions/{session_id}/complete",
    summary="Not offered: the buyer pays on the store's Shopify checkout (continue_url)",
    status_code=409,
)
async def complete_session(
    session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenProviderDep,
) -> JSONResponse:
    """Always rejected: the buyer completes payment on the store's own Shopify checkout via
    `continue_url`."""
    h = await _handler(request, session, settings, tokens)
    raise handoff_error(await h.cart(session_id))
