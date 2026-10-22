"""Mounts the MCP server at `/mcp` behind agent API-key authentication."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import State
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.agent import presented_key, resolve_agent
from app.core.config import get_settings
from app.jobs.catalog_audit import shopify_client_for
from app.models import AgentProtocol, Merchant, MerchantStatus
from app.protocols.base import ProtocolError
from app.protocols.mcp_server import ToolScope, build_mcp_server
from app.repositories.carts import CartRepository
from app.services.cart_manager import CartManager

STATE_KEY = "acg_agent"


@dataclass(frozen=True)
class McpAgent:
    """The authenticated MCP agent behind a request: its client, merchant and scopes."""

    client_id: uuid.UUID
    merchant_id: uuid.UUID
    scopes: frozenset[str]


class AgentKeyAuth:
    """ASGI wrapper: rejects requests without a valid MCP agent key, otherwise records the agent
    in the request scope's state, where tool calls read it back."""

    def __init__(self, app: ASGIApp, state: State) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate the request, then pass it to the MCP app or answer 401/403."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        async with self.state.db.session() as session:
            try:
                principal = await resolve_agent(
                    session, presented_key(Request(scope)), AgentProtocol.MCP, None
                )
            except ProtocolError as exc:
                response = JSONResponse(
                    {"error": "invalid_token", "error_description": exc.message},
                    exc.status,
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
                await response(scope, receive, send)
                return
            agent = McpAgent(
                principal.client.id, principal.merchant.id, frozenset(principal.client.scopes)
            )
        scope.setdefault("state", {})[STATE_KEY] = agent
        await self.app(scope, receive, send)


def _agent(ctx: Context) -> McpAgent:
    request: Any = ctx.request_context.request
    agent = (getattr(request, "scope", None) or {}).get("state", {}).get(STATE_KEY)
    if not isinstance(agent, McpAgent):
        raise ToolError("unauthenticated")
    return agent


def mount_mcp(routes: list[Any], state: State) -> MCPServer:
    """Build the server, add its `/mcp` route, and return it (its session manager must be run
    in the app lifespan)."""

    @asynccontextmanager
    async def open_scope(ctx: Context) -> AsyncIterator[ToolScope]:
        agent = _agent(ctx)
        settings = get_settings()
        async with state.db.session() as session:
            merchant = await session.get(Merchant, agent.merchant_id)
            if merchant is None or merchant.status is not MerchantStatus.ACTIVE:
                raise ToolError("store not available")
            client = shopify_client_for(merchant, state.http_client, state.shopify_tokens, settings)
            yield ToolScope(
                merchant_id=merchant.id,
                agent_client_id=agent.client_id,
                scopes=agent.scopes,
                shopify=client,
                carts=CartManager(CartRepository(session), client, merchant.shop_domain),
            )

    server = build_mcp_server(open_scope)
    # Stateless JSON mode: no server-side MCP sessions, so any replica can serve any call.
    # Host-header (DNS rebinding) checks are for local servers; this one requires an API key.
    server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    routes.append(
        Route("/mcp", endpoint=AgentKeyAuth(StreamableHTTPASGIApp(server.session_manager), state))
    )
    return server
