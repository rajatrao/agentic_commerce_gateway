"""Payment gateway seam. `StripeGateway` charges delegated agent payment tokens.

Supported credentials:
- Stripe shared payment tokens (`spt_...`) from ACP / UCP agents, used once via
  `payment_method_data[shared_payment_granted_token]` on a confirmed PaymentIntent.
- Stripe PaymentMethod ids (`pm_...`), only with a test-mode key, so the flow can be exercised
  with `pm_card_visa` and friends before SPT access is enabled.

The token itself is never logged or stored; callers keep only the PaymentIntent id.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import stripe
import structlog

log = structlog.get_logger(__name__)


class PaymentConfigError(Exception):
    """Payments are not configured for this store."""


@dataclass(frozen=True)
class ChargeResult:
    succeeded: bool
    payment_ref: str | None = None
    # Agent-facing codes: payment_declined, requires_3ds, invalid_token, payment_error.
    code: str | None = None
    message: str | None = None


class PaymentGateway(Protocol):
    async def charge(
        self,
        *,
        amount_minor: int,
        currency: str,
        token: str,
        idempotency_key: str,
        metadata: dict[str, str],
        account: str | None,
    ) -> ChargeResult: ...

    async def refund(
        self, *, payment_ref: str, idempotency_key: str, account: str | None
    ) -> None: ...


class StripeGateway:
    def __init__(self, secret_key: str, api_version: str) -> None:
        if not secret_key:
            raise PaymentConfigError("STRIPE_SECRET_KEY is not configured")
        self._client = stripe.StripeClient(
            secret_key, stripe_version=api_version, http_client=stripe.HTTPXClient()
        )
        self._test_mode = secret_key.startswith(("sk_test_", "rk_test_"))

    @staticmethod
    def _options(idempotency_key: str, account: str | None) -> Any:
        options: dict[str, Any] = {"idempotency_key": idempotency_key}
        if account:
            options["stripe_account"] = account
        return options

    async def charge(
        self,
        *,
        amount_minor: int,
        currency: str,
        token: str,
        idempotency_key: str,
        metadata: dict[str, str],
        account: str | None,
    ) -> ChargeResult:
        params: dict[str, Any] = {
            "amount": amount_minor,
            "currency": currency.lower(),
            "confirm": True,
            "metadata": metadata,
            "description": f"Agentic Commerce order {metadata.get('transaction_id', '')}".strip(),
            # Agent checkouts never redirect: anything needing the buyer present fails cleanly.
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        }
        if token.startswith("spt_"):
            params["payment_method_data"] = {"shared_payment_granted_token": token}
        elif token.startswith("pm_") and self._test_mode:
            params["payment_method"] = token
        else:
            return ChargeResult(False, code="invalid_token", message="Unsupported payment token.")

        try:
            intent = await self._client.v1.payment_intents.create_async(
                params,  # type: ignore[arg-type]
                self._options(idempotency_key, account),
            )
        except stripe.CardError as exc:
            message = exc.user_message or "The card was declined."
            return ChargeResult(False, code="payment_declined", message=message)
        except stripe.InvalidRequestError as exc:
            log.warning("payment.invalid_request", code=exc.code, param=exc.param)
            return ChargeResult(
                False, code="invalid_token", message="The payment token is invalid."
            )
        except stripe.StripeError as exc:
            log.warning("payment.stripe_error", error_type=type(exc).__name__, code=exc.code)
            message = "The payment could not be processed."
            return ChargeResult(False, code="payment_error", message=message)

        if intent.status == "succeeded":
            return ChargeResult(True, payment_ref=intent.id)

        # requires_action (3DS) and anything else: release the authorization and report.
        try:
            await self._client.v1.payment_intents.cancel_async(
                intent.id, options=self._options(f"{idempotency_key}-cancel", account)
            )
        except stripe.StripeError:
            log.warning("payment.cancel_failed", payment_ref=intent.id)
        if intent.status == "requires_action":
            message = "The payment needs buyer authentication."
            return ChargeResult(False, intent.id, "requires_3ds", message)
        return ChargeResult(False, intent.id, "payment_declined", "The payment did not complete.")

    async def refund(self, *, payment_ref: str, idempotency_key: str, account: str | None) -> None:
        await self._client.v1.refunds.create_async(
            {"payment_intent": payment_ref}, self._options(idempotency_key, account)
        )
