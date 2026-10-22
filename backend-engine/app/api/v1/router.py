"""Assembles every v1 router into the API router mounted by `create_app`."""

from fastapi import APIRouter

from app.api.v1 import (
    acp,
    agent_clients,
    agent_gateway,
    audits,
    catalog,
    internal,
    merchant,
    orders,
    ucp,
    webhooks,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(merchant.router)
api_router.include_router(audits.router)
api_router.include_router(catalog.router)
api_router.include_router(agent_gateway.router)
api_router.include_router(webhooks.router)
api_router.include_router(agent_clients.router)
api_router.include_router(acp.router)
api_router.include_router(ucp.router)
api_router.include_router(orders.router)

internal_router = internal.router

# Mounted at the root: /.well-known/ucp
public_router = ucp.discovery_router
