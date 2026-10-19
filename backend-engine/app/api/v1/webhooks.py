"""Shopify webhooks, verified by HMAC and de-duplicated by `X-Shopify-Webhook-Id`."""

import json
from typing import Annotated

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.deps import SessionDep, SettingsDep
from app.core.security import verify_shopify_webhook_hmac
from app.repositories.merchants import MerchantRepository
from app.repositories.webhooks import WebhookEventRepository

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = structlog.get_logger(__name__)

# Topics we act on. GDPR topics are mandatory for every public Shopify app.
HANDLED_TOPICS = {
    "app/uninstalled",
    "app/scopes_update",
    "customers/data_request",
    "customers/redact",
    "shop/redact",
}


@router.post("/shopify", status_code=status.HTTP_200_OK)
async def shopify_webhook(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    x_shopify_hmac_sha256: Annotated[str | None, Header()] = None,
    x_shopify_topic: Annotated[str | None, Header()] = None,
    x_shopify_shop_domain: Annotated[str | None, Header()] = None,
    x_shopify_webhook_id: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    raw = await request.body()
    if not verify_shopify_webhook_hmac(raw, x_shopify_hmac_sha256, settings.shopify_api_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid hmac")
    if not (x_shopify_topic and x_shopify_shop_domain and x_shopify_webhook_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing shopify headers")

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid json") from exc

    events = WebhookEventRepository(session)
    if not await events.record_if_new(
        x_shopify_webhook_id, x_shopify_shop_domain, x_shopify_topic, payload
    ):
        return {"status": "duplicate"}

    if x_shopify_topic == "app/uninstalled" or x_shopify_topic == "shop/redact":
        await MerchantRepository(session).mark_uninstalled(x_shopify_shop_domain)
    elif x_shopify_topic not in HANDLED_TOPICS:
        log.info("webhook.unhandled_topic", topic=x_shopify_topic, shop=x_shopify_shop_domain)
    # customers/* : the gateway keeps no Shopify customer records.

    await events.mark_processed(x_shopify_webhook_id)
    return {"status": "ok"}
