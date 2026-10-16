"""`POST /api/v1/agent-checkout`: protocol-neutral payment for a cart built over UCP, ACP or MCP.

Accepts either delegated-payment payload shape:
- ACP: `{"checkout_id": "...", "payment_data": {"token": "spt_...", "provider": "stripe"}}`
- UCP: `{"checkout_id": "...", "payment": {"instruments": [{"handler_id": "stripe",
  "credential": {"token": "spt_..."}}]}}`

Authenticated with the agent's API key (any protocol, `checkout` scope; only the agent that
created the cart can pay for it) and an `Idempotency-Key`. Errors use the ACP error shape.
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.agent import CheckoutHandler, Rendered
from app.api.deps import SessionDep, SettingsDep, TokenProviderDep
from app.models import AgentTransaction, TransactionStatus
from app.protocols.acp_adapter import AcpAdapter
from app.protocols.base import PaymentPayload, ProtocolError, to_minor
from app.protocols.ucp_adapter import UcpAdapter

router = APIRouter(tags=["agent-checkout"])
acp, ucp = AcpAdapter(), UcpAdapter()

OUTCOME_STATUS = {
    TransactionStatus.ORDER_CREATED: 201,
    TransactionStatus.FAILED: 402,
    TransactionStatus.REFUNDED: 502,
    TransactionStatus.CAPTURED: 502,
}


def parse_payment(body: Any) -> tuple[str, PaymentPayload]:
    if not isinstance(body, dict) or not isinstance(body.get("checkout_id"), str):
        raise ProtocolError(400, "invalid", "checkout_id is required", "$.checkout_id")
    payload = {k: v for k, v in body.items() if k != "checkout_id"}
    if "payment_data" in payload:
        return body["checkout_id"], acp.parse_complete(payload)
    if "payment" in payload:
        return body["checkout_id"], ucp.parse_complete(payload)
    raise ProtocolError(400, "invalid", "payment_data (ACP) or payment (UCP) is required")


def render_transaction(txn: AgentTransaction, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "transaction_id": str(txn.id),
        "checkout_id": str(txn.cart_id),
        "status": txn.status.value,
        "amount": to_minor(txn.amount, txn.currency),
        "currency": txn.currency.lower(),
        "payment_ref": txn.payment_ref,
        "order": (
            {"id": txn.order_gid, "name": txn.order_name, "status_url": txn.order_status_url}
            if txn.order_gid
            else None
        ),
        "error": {"code": txn.error_code, "message": txn.error_detail} if txn.error_code else None,
        "messages": [
            {k: m[k] for k in ("type", "code", "content", "path") if m.get(k)} for m in messages
        ],
    }


@router.post(
    "/agent-checkout",
    summary="Pay for an agent cart with a delegated payment token (Stripe SPT)",
    responses={
        201: {"description": "Order created (Paid / Unfulfilled)"},
        402: {"description": "Payment declined or checkout not ready"},
    },
)
async def agent_checkout(
    request: Request, session: SessionDep, settings: SettingsDep, tokens: TokenProviderDep
) -> JSONResponse:
    h = await CheckoutHandler.create(acp, request, session, settings, tokens, any_protocol=True)
    body = await h.body()

    async def op() -> Rendered:
        checkout_id, payment = parse_payment(body)
        outcome = await h.checkout(checkout_id, payment)
        txn = outcome.transaction
        return Rendered(
            OUTCOME_STATUS.get(txn.status, 402), render_transaction(txn, outcome.cart.messages)
        )

    return await h.mutate(body, op)
