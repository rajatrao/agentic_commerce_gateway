"""Agentic Commerce Protocol checkout endpoints (seller side)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.agent import CheckoutHandler, Rendered
from app.api.deps import SessionDep, SettingsDep, TokenProviderDep
from app.models import AgentCart
from app.protocols.acp_adapter import AcpAdapter

router = APIRouter(prefix="/acp", tags=["acp"])
adapter = AcpAdapter()


def _handler(
    request: Request, session: SessionDep, settings: SettingsDep, tokens: TokenProviderDep
) -> CheckoutHandler:
    return CheckoutHandler(adapter, request, session, settings, tokens)


@router.post("/checkout_sessions", status_code=201, summary="Create an ACP checkout session")
async def create_session(
    request: Request, session: SessionDep, settings: SettingsDep, tokens: TokenProviderDep
) -> JSONResponse:
    h = _handler(request, session, settings, tokens)
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
    h = _handler(request, session, settings, tokens)
    return h.respond(h.cart(session_id))


@router.post("/checkout_sessions/{session_id}", summary="Update an ACP checkout session")
async def update_session(
    session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenProviderDep,
) -> JSONResponse:
    h = _handler(request, session, settings, tokens)
    body = await h.body()

    async def op() -> AgentCart:
        changes = adapter.parse_update(body)
        return await h.carts.update(h.cart(session_id), changes)

    return await h.mutate(body, op)


@router.post("/checkout_sessions/{session_id}/cancel", summary="Cancel an ACP checkout session")
async def cancel_session(
    session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenProviderDep,
) -> JSONResponse:
    h = _handler(request, session, settings, tokens)

    async def op() -> AgentCart:
        return h.cancel(h.cart(session_id))

    return await h.mutate({}, op)


@router.post(
    "/checkout_sessions/{session_id}/complete",
    summary="Pay with a delegated payment token and create the order",
)
async def complete_session(
    session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenProviderDep,
) -> JSONResponse:
    h = _handler(request, session, settings, tokens)
    body = await h.body()

    async def op() -> Rendered:
        outcome = await h.checkout(session_id, adapter.parse_complete(body))
        return Rendered(200, adapter.render_completion(outcome.cart, outcome.transaction))

    return await h.mutate(body, op)
