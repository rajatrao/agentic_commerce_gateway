"""Universal Commerce Protocol checkout endpoints (REST binding)."""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.agent import CheckoutHandler
from app.api.deps import SessionDep, SettingsDep, TokenProviderDep
from app.models import AgentCart
from app.protocols.base import ProtocolError
from app.protocols.ucp_adapter import UCP_VERSION, UcpAdapter, envelope

router = APIRouter(prefix="/ucp", tags=["ucp"])
discovery_router = APIRouter(tags=["ucp"])
adapter = UcpAdapter()


@discovery_router.get("/.well-known/ucp", summary="UCP business profile (discovery)")
def ucp_profile(request: Request, settings: SettingsDep) -> dict[str, Any]:
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    profile = envelope()
    profile["services"] = {
        "dev.ucp.shopping": [
            {
                "version": UCP_VERSION,
                "spec": "https://ucp.dev/specification/overview",
                "rest": {"endpoint": f"{base}/api/v1/ucp"},
                "mcp": {"endpoint": f"{base}/mcp"},
            }
        ]
    }
    # Payment handlers are declared once delegated payments ship (Phase 4).
    profile["payment_handlers"] = {}
    return {"ucp": profile}


def _handler(
    request: Request, session: SessionDep, settings: SettingsDep, tokens: TokenProviderDep
) -> CheckoutHandler:
    return CheckoutHandler(adapter, request, session, settings, tokens)


@router.post("/checkout-sessions", status_code=201, summary="Create a UCP checkout session")
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
        )

    return await h.mutate(body, op, status=201)


@router.get("/checkout-sessions/{session_id}", summary="Get a UCP checkout session")
async def get_session(
    session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenProviderDep,
) -> JSONResponse:
    h = _handler(request, session, settings, tokens)
    return h.respond(h.cart(session_id))


@router.put("/checkout-sessions/{session_id}", summary="Update a UCP checkout session")
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


@router.post("/checkout-sessions/{session_id}/cancel", summary="Cancel a UCP checkout session")
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


@router.post("/checkout-sessions/{session_id}/complete", summary="Complete (Phase 4)")
async def complete_session(
    session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenProviderDep,
) -> JSONResponse:
    h = _handler(request, session, settings, tokens)
    h.cart(session_id)
    raise ProtocolError(501, "not_implemented", "payment completion is not available yet")
