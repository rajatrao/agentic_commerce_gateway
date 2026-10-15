"""Turns a ready agent cart plus a delegated payment token into a paid Shopify order.

Flow (every step appends a `transaction_events` row):

1. Lock the cart and open a `pending` transaction. A cart can only have one checkout in flight
   and only one successful one; the same idempotency key replays the earlier transaction.
2. Re-check stock and re-price through Shopify (`CartManager.update`). A cart that is no longer
   ready fails here, before any money moves.
3. `draftOrderCreate` with the cart's lines, buyer, address and shipping rate, tagged
   `Origin: Agentic Commerce`, holding stock for a few minutes. The draft total must equal the
   price the agent saw, otherwise nothing is charged.
4. Charge the delegated token (Stripe shared payment token) for exactly that total.
5. `draftOrderComplete` marks the order paid, giving a **Paid / Unfulfilled** order. If this
   fails after the charge, the payment is refunded automatically.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog

from app.models import AgentCart, AgentTransaction, CartStatus, TransactionStatus
from app.models.base import utcnow
from app.repositories.transactions import TransactionRepository
from app.services.cart_manager import (
    BuyerInput,
    CartChanges,
    CartError,
    CartManager,
    ShopifyQuery,
    draft_order_input,
    money,
    sellable_lines,
)
from app.services.money import to_minor
from app.services.payment_gateway import PaymentConfigError, PaymentGateway
from app.services.shopify_client import ShopifyError

log = structlog.get_logger(__name__)

ORDER_TAGS = ["Origin: Agentic Commerce"]

DRAFT_CREATE = """
mutation AgentDraftCreate($input: DraftOrderInput!) {
  draftOrderCreate(input: $input) {
    draftOrder {
      id
      name
      totalPriceSet { shopMoney { amount currencyCode } }
    }
    userErrors { field message }
  }
}
"""

DRAFT_COMPLETE = """
mutation AgentDraftComplete($id: ID!) {
  draftOrderComplete(id: $id) {
    draftOrder {
      id
      order { id name statusPageUrl displayFinancialStatus displayFulfillmentStatus }
    }
    userErrors { field message }
  }
}
"""

DRAFT_DELETE = """
mutation AgentDraftDelete($input: DraftOrderDeleteInput!) {
  draftOrderDelete(input: $input) { deletedId userErrors { field message } }
}
"""


class CheckoutError(Exception):
    """The checkout cannot start. `code`: not_found, in_progress, already_completed,
    not_modifiable, payments_not_configured."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PaymentRequest:
    token: str
    idempotency_key: str
    agent_client_id: uuid.UUID | None = None
    provider: str = "stripe"
    buyer: BuyerInput | None = None


@dataclass(frozen=True)
class CheckoutOutcome:
    transaction: AgentTransaction
    cart: AgentCart

    @property
    def succeeded(self) -> bool:
        return self.transaction.status is TransactionStatus.ORDER_CREATED


class PaymentProcessor:
    def __init__(
        self,
        carts: CartManager,
        transactions: TransactionRepository,
        shopify: ShopifyQuery,
        gateway: Callable[[], PaymentGateway],
        *,
        stripe_account: str | None = None,
        reservation_minutes: int = 10,
    ) -> None:
        self.carts = carts
        self.txns = transactions
        self.shopify = shopify
        self._gateway = gateway
        self.stripe_account = stripe_account
        self.reservation = timedelta(minutes=reservation_minutes)

    async def checkout(
        self,
        merchant_id: uuid.UUID,
        cart_id: uuid.UUID,
        request: PaymentRequest,
    ) -> CheckoutOutcome:
        if request.provider != "stripe":
            raise CheckoutError("invalid", f"unsupported payment provider {request.provider!r}")
        key = f"{request.agent_client_id or 'merchant'}:{request.idempotency_key}"

        replay = self.txns.by_idempotency_key(merchant_id, key)
        if replay is not None:
            if replay.cart_id != cart_id:
                raise CheckoutError("invalid", "idempotency key was used for another checkout")
            return CheckoutOutcome(replay, self.carts.get(merchant_id, cart_id))

        cart = self.txns.lock_cart(merchant_id, cart_id)
        if cart is None or (
            request.agent_client_id
            and cart.agent_client_id
            and cart.agent_client_id != request.agent_client_id
        ):
            raise CheckoutError("not_found", "checkout session not found")
        if done := self.txns.completed_for_cart(cart):
            raise CheckoutError("already_completed", f"already paid as order {done.order_name}")
        if self.txns.blocking_for_cart(cart.id):
            raise CheckoutError("in_progress", "a payment for this checkout is already running")
        if cart.status not in (CartStatus.OPEN, CartStatus.READY):
            raise CheckoutError("not_modifiable", f"checkout session is {cart.status.value}")
        try:
            gateway = self._gateway()
        except PaymentConfigError as exc:
            raise CheckoutError(
                "payments_not_configured", "this store does not take agent payments yet"
            ) from exc

        txn = self.txns.save(
            AgentTransaction(
                merchant_id=merchant_id,
                cart_id=cart.id,
                agent_client_id=request.agent_client_id,
                protocol=cart.protocol,
                idempotency_key=key,
                payment_provider=request.provider,
                currency=cart.currency,
                amount=cart.total,
            ),
            "checkout_started",
            token_type=request.token.split("_", 1)[0],
        )  # commits, releasing the cart lock; the pending row now blocks other checkouts

        try:
            return await self._pay(cart, txn, gateway, request)
        except Exception as exc:
            # Never leave a pending row behind: it would block this cart forever.
            if txn.status is TransactionStatus.PENDING:
                self.txns.session.rollback()
                self._fail(txn, txn.error_code or "internal_error", str(exc) or type(exc).__name__)
            raise

    async def _pay(
        self,
        cart: AgentCart,
        txn: AgentTransaction,
        gateway: PaymentGateway,
        request: PaymentRequest,
    ) -> CheckoutOutcome:
        # 2. Fresh stock and price.
        try:
            cart = await self.carts.update(cart, CartChanges(buyer=request.buyer))
        except (CartError, ShopifyError) as exc:
            self._fail(txn, "reprice_failed", str(exc))
            raise
        if cart.status is not CartStatus.READY:
            return self._fail_outcome(txn, cart, "not_ready", "checkout is not ready for payment")
        txn.amount, txn.currency = cart.total, cart.currency

        # 3. Draft order at the agreed price.
        try:
            draft = await self._create_draft(cart, txn)
        except ShopifyError as exc:
            self._fail(txn, "draft_order_failed", str(exc))
            raise
        txn.draft_order_gid = draft["id"]
        drafted_total = money(draft["totalPriceSet"]["shopMoney"]["amount"])
        self.txns.save(
            txn, "draft_order_created", draft_order=draft["id"], total=str(drafted_total)
        )
        if drafted_total != money(cart.total):
            await self._delete_draft(txn)
            return self._fail_outcome(
                txn,
                cart,
                "price_changed",
                f"The total changed to {drafted_total} {cart.currency}; review and retry.",
                path="payment",
            )

        # 4. Charge exactly the drafted total.
        charge = await gateway.charge(
            amount_minor=to_minor(drafted_total, cart.currency),
            currency=cart.currency,
            token=request.token,
            idempotency_key=f"acg-charge-{txn.id}",
            metadata={
                "transaction_id": str(txn.id),
                "checkout_id": str(cart.id),
                "draft_order": draft["id"],
                "origin": "agentic-commerce",
            },
            account=self.stripe_account,
        )
        if not charge.succeeded:
            txn.payment_ref = charge.payment_ref
            await self._delete_draft(txn)
            return self._fail_outcome(
                txn,
                cart,
                charge.code or "payment_declined",
                charge.message or "The payment was declined.",
                path="payment",
            )
        txn.status, txn.payment_ref = TransactionStatus.CAPTURED, charge.payment_ref
        self.txns.save(
            txn,
            "payment_captured",
            payment_ref=charge.payment_ref,
            amount=str(drafted_total),
            currency=cart.currency,
        )

        # 5. Turn the draft into a paid order; refund if Shopify refuses.
        try:
            order = await self._complete_draft(draft["id"])
        except ShopifyError as exc:
            log.error("checkout.order_failed", transaction_id=str(txn.id), error=str(exc))
            await self._refund(gateway, txn, reason=str(exc))
            await self._delete_draft(txn)
            return self._fail_outcome(
                txn,
                cart,
                "order_failed",
                "The store could not create the order; the payment was refunded.",
                status=txn.status,
                path="payment",
            )

        txn.status = TransactionStatus.ORDER_CREATED
        txn.order_gid, txn.order_name = order["id"], order.get("name")
        txn.order_status_url = order.get("statusPageUrl")
        self.txns.save(
            txn,
            "order_created",
            order=order["id"],
            name=order.get("name"),
            financial_status=order.get("displayFinancialStatus"),
            fulfillment_status=order.get("displayFulfillmentStatus"),
        )
        cart.status = CartStatus.COMPLETED
        cart.messages = []
        cart.updated_at = utcnow()
        cart = self.carts.carts.save(cart)
        log.info("checkout.completed", transaction_id=str(txn.id), order=order.get("name"))
        return CheckoutOutcome(txn, cart)

    # ------------------------------------------------------------------ Shopify steps

    async def _create_draft(self, cart: AgentCart, txn: AgentTransaction) -> dict[str, Any]:
        draft_input = draft_order_input(cart, sellable_lines(cart), cart.shipping_rate_handle)
        if cart.shipping_address:
            draft_input["billingAddress"] = draft_input["shippingAddress"]
        protocol = cart.protocol.value.upper()
        draft_input.update(
            tags=[*ORDER_TAGS, f"Agent protocol: {protocol}"],
            note=f"Placed by an AI agent via Agentic Commerce Gateway ({protocol}).",
            customAttributes=[
                {"key": "acg_transaction_id", "value": str(txn.id)},
                {"key": "acg_checkout_id", "value": str(cart.id)},
            ],
            reserveInventoryUntil=(utcnow() + self.reservation).isoformat(),
        )
        result = (await self.shopify.query(DRAFT_CREATE, {"input": draft_input}))[
            "draftOrderCreate"
        ]
        if result.get("userErrors") or not result.get("draftOrder"):
            raise ShopifyError(_errors(result))
        return result["draftOrder"]  # type: ignore[no-any-return]

    async def _complete_draft(self, draft_id: str) -> dict[str, Any]:
        result = (await self.shopify.query(DRAFT_COMPLETE, {"id": draft_id}))["draftOrderComplete"]
        order = ((result or {}).get("draftOrder") or {}).get("order")
        if result.get("userErrors") or not order:
            raise ShopifyError(_errors(result))
        return order  # type: ignore[no-any-return]

    async def _delete_draft(self, txn: AgentTransaction) -> None:
        if not txn.draft_order_gid:
            return
        try:
            await self.shopify.query(DRAFT_DELETE, {"input": {"id": txn.draft_order_gid}})
        except ShopifyError as exc:
            log.warning("checkout.draft_delete_failed", draft=txn.draft_order_gid, error=str(exc))

    async def _refund(self, gateway: PaymentGateway, txn: AgentTransaction, reason: str) -> None:
        assert txn.payment_ref
        try:
            await gateway.refund(
                payment_ref=txn.payment_ref,
                idempotency_key=f"acg-refund-{txn.id}",
                account=self.stripe_account,
            )
        except Exception as exc:  # noqa: BLE001 - must record, never lose, a stuck capture
            log.error("checkout.refund_failed", transaction_id=str(txn.id), error=str(exc))
            txn.error_code, txn.error_detail = "refund_failed", f"{reason}; refund failed: {exc}"
            self.txns.save(txn, "refund_failed", error=str(exc))
            return
        txn.status = TransactionStatus.REFUNDED
        self.txns.save(txn, "payment_refunded", payment_ref=txn.payment_ref, reason=reason)

    # ------------------------------------------------------------------ failures

    def _fail(self, txn: AgentTransaction, code: str, detail: str) -> None:
        txn.status, txn.error_code, txn.error_detail = TransactionStatus.FAILED, code, detail[:1000]
        self.txns.save(txn, "checkout_failed", code=code, detail=detail[:1000])

    def _fail_outcome(
        self,
        txn: AgentTransaction,
        cart: AgentCart,
        code: str,
        message: str,
        *,
        status: TransactionStatus = TransactionStatus.FAILED,
        path: str | None = None,
    ) -> CheckoutOutcome:
        if txn.error_code != "refund_failed":
            txn.error_code, txn.error_detail = code, message
        txn.status = status
        self.txns.save(txn, "checkout_failed", code=code, detail=message)
        if path:
            # Shown to the agent until the next change re-prices the cart.
            cart.messages = [
                *cart.messages,
                {"type": "error", "code": code, "content": message, "path": path},
            ]
            cart = self.carts.carts.save(cart)
        return CheckoutOutcome(txn, cart)


def _errors(result: dict[str, Any] | None) -> str:
    errors = (result or {}).get("userErrors") or []
    return "; ".join(e.get("message", "") for e in errors) or "no result"
