from urllib.parse import parse_qs

import pytest
import respx

from app.services.payment_gateway import PaymentConfigError, StripeGateway

PI_URL = "https://api.stripe.com/v1/payment_intents"


def intent(status: str) -> dict:
    return {"id": "pi_123", "object": "payment_intent", "status": status}


def gateway(key: str = "sk_test_abc") -> StripeGateway:
    return StripeGateway(key, "2026-09-30.preview")


async def charge(gw: StripeGateway, token: str = "spt_abc", account: str | None = "acct_9"):
    return await gw.charge(
        amount_minor=13750,
        currency="USD",
        token=token,
        idempotency_key="acg-charge-1",
        metadata={"transaction_id": "t1"},
        account=account,
    )


@respx.mock
async def test_shared_payment_token_charge() -> None:
    route = respx.post(PI_URL).respond(200, json=intent("succeeded"))
    result = await charge(gateway())
    assert result.succeeded and result.payment_ref == "pi_123"

    request = route.calls.last.request
    form = parse_qs(request.content.decode())
    assert form["amount"] == ["13750"] and form["currency"] == ["usd"]
    assert form["confirm"] == ["true"]
    assert form["payment_method_data[shared_payment_granted_token]"] == ["spt_abc"]
    assert request.headers["Stripe-Version"] == "2026-09-30.preview"
    assert request.headers["Idempotency-Key"] == "acg-charge-1"
    assert request.headers["Stripe-Account"] == "acct_9"


@respx.mock
async def test_card_decline() -> None:
    respx.post(PI_URL).respond(
        402,
        json={
            "error": {
                "type": "card_error",
                "code": "card_declined",
                "message": "Your card was declined.",
            }
        },
    )
    result = await charge(gateway())
    assert not result.succeeded
    assert (result.code, result.message) == ("payment_declined", "Your card was declined.")


@respx.mock
async def test_requires_action_is_cancelled() -> None:
    respx.post(PI_URL).respond(200, json=intent("requires_action"))
    cancel = respx.post(f"{PI_URL}/pi_123/cancel").respond(200, json=intent("canceled"))
    result = await charge(gateway())
    assert (result.succeeded, result.code) == (False, "requires_3ds")
    assert cancel.called


@respx.mock
async def test_payment_method_ids_only_in_test_mode() -> None:
    route = respx.post(PI_URL).respond(200, json=intent("succeeded"))
    assert (await charge(gateway(), token="pm_card_visa")).succeeded
    assert parse_qs(route.calls.last.request.content.decode())["payment_method"] == ["pm_card_visa"]
    live = await charge(gateway("sk_live_abc"), token="pm_card_visa")
    assert (live.succeeded, live.code) == (False, "invalid_token")
    assert route.call_count == 1


@respx.mock
async def test_refund() -> None:
    route = respx.post("https://api.stripe.com/v1/refunds").respond(
        200, json={"id": "re_1", "object": "refund"}
    )
    await gateway().refund(payment_ref="pi_123", idempotency_key="acg-refund-1", account=None)
    assert parse_qs(route.calls.last.request.content.decode()) == {"payment_intent": ["pi_123"]}


def test_requires_key() -> None:
    with pytest.raises(PaymentConfigError):
        StripeGateway("", "v")
