"""Shared contract for the agent protocol adapters.

An adapter is a pure translation layer: it validates a protocol's request body into the
protocol-neutral `CartChanges` consumed by `services.cart_manager`, and renders an `AgentCart`
back into that protocol's response shape. Adapters never touch the database or Shopify.

Payment never happens here. A ready cart carries `checkout_url` (the merchant's own Shopify
checkout), rendered as `continue_url`; a protocol `complete` call is answered with
`handoff_error`, which points the agent at that URL.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from app.models import AgentCart, AgentProtocol, CartStatus
from app.services.cart_manager import CartChanges, CartError
from app.services.money import to_minor

__all__ = [
    "EMAIL_PATTERN",
    "ProtocolAdapter",
    "ProtocolError",
    "split_name",
    "to_minor",
]

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class ProtocolError(Exception):
    """A request the adapter or cart rejected, rendered in the protocol's own error format."""

    def __init__(self, status: int, code: str, message: str, param: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param

    @classmethod
    def from_cart_error(cls, exc: CartError) -> "ProtocolError":
        status = {"not_found": 404, "not_modifiable": 405}.get(exc.code, 400)
        return cls(status, exc.code, exc.message, exc.path)

    @classmethod
    def from_validation(cls, exc: ValidationError) -> "ProtocolError":
        first = exc.errors()[0]
        path = "$." + ".".join(str(p) for p in first["loc"]) if first["loc"] else None
        return cls(400, "invalid", first["msg"], path)


class ProtocolAdapter(ABC):
    protocol: ClassVar[AgentProtocol]
    create_model: ClassVar[type[BaseModel]]
    update_model: ClassVar[type[BaseModel]]

    def parse_create(self, body: Any) -> CartChanges:
        return self._changes(self._validate(self.create_model, body))

    def parse_update(self, body: Any) -> CartChanges:
        return self._changes(self._validate(self.update_model, body))

    @staticmethod
    def _validate(model: type[BaseModel], body: Any) -> BaseModel:
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise ProtocolError.from_validation(exc) from exc

    @abstractmethod
    def _changes(self, request: BaseModel) -> CartChanges: ...

    @abstractmethod
    def render(self, cart: AgentCart) -> dict[str, Any]: ...

    @abstractmethod
    def render_error(self, error: ProtocolError) -> dict[str, Any]: ...


def handoff_error(cart: AgentCart) -> ProtocolError:
    """The answer to `complete`: payment happens on the merchant's Shopify checkout."""
    if cart.status is CartStatus.READY and cart.checkout_url:
        return ProtocolError(
            409,
            "requires_escalation",
            "Payment happens on the store's own checkout. Send the buyer to continue_url: "
            f"{cart.checkout_url}",
            "$.continue_url",
        )
    if cart.status is CartStatus.OPEN:
        return ProtocolError(409, "not_ready", "checkout is not ready yet; see messages")
    return ProtocolError(405, "not_modifiable", f"checkout session is {cart.status.value}")


def split_name(name: str | None) -> tuple[str | None, str | None]:
    if not name or not name.strip():
        return None, None
    first, _, last = name.strip().partition(" ")
    return first, last.strip() or None
