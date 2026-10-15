from fastapi import APIRouter

from app.api.v1 import agent_gateway, audits, internal, merchant, webhooks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(merchant.router)
api_router.include_router(audits.router)
api_router.include_router(agent_gateway.router)
api_router.include_router(webhooks.router)

internal_router = internal.router
