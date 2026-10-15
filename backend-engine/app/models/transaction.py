import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Column, Identity, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.base import pg_enum, tz_column, utcnow
from app.models.merchant import AgentProtocol


class CartStatus(StrEnum):
    OPEN = "open"
    READY = "ready"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELED = "canceled"


class TransactionStatus(StrEnum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    ORDER_CREATED = "order_created"
    FAILED = "failed"
    REFUNDED = "refunded"


def _money() -> Any:
    return Field(default=Decimal("0"), sa_column=Column(Numeric(12, 2), nullable=False))


class AgentCart(SQLModel, table=True):
    __tablename__ = "agent_carts"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    protocol: AgentProtocol = Field(
        sa_column=Column(pg_enum(AgentProtocol, "agent_protocol"), nullable=False)
    )
    agent_client_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_clients.id", index=True, ondelete="SET NULL"
    )
    external_session_id: str | None = Field(default=None, index=True)
    lines: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]")
    )
    buyer: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    shipping_address: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Rates Shopify offered for the current address, and the one the agent (or we) picked.
    shipping_options: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]")
    )
    shipping_rate_handle: str | None = None
    # Problems an agent must resolve before checkout (out of stock, missing address, ...).
    messages: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]")
    )
    subtotal: Decimal = _money()
    shipping: Decimal = _money()
    tax: Decimal = _money()
    total: Decimal = _money()
    currency: str = Field(default="USD", sa_column=Column(String(3), nullable=False))
    status: CartStatus = Field(
        default=CartStatus.OPEN,
        sa_column=Column(pg_enum(CartStatus, "cart_status"), nullable=False),
    )
    expires_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())


class IdempotencyRecord(SQLModel, table=True):
    """Stored response for an agent request carrying an `Idempotency-Key` header.

    `status_code` is NULL while the first request is still being processed, so a concurrent
    retry gets a 409 instead of running the operation twice.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("agent_client_id", "key"),)

    id: int | None = Field(default=None, sa_column=Column(BigInteger, Identity(), primary_key=True))
    agent_client_id: uuid.UUID = Field(foreign_key="agent_clients.id", ondelete="CASCADE")
    key: str = Field(sa_column=Column(String(255), nullable=False))
    request_hash: str
    status_code: int | None = None
    response_body: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column(index=True))


class AgentTransaction(SQLModel, table=True):
    __tablename__ = "agent_transactions"
    __table_args__ = (UniqueConstraint("merchant_id", "idempotency_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    merchant_id: uuid.UUID = Field(foreign_key="merchants.id", index=True, ondelete="CASCADE")
    cart_id: uuid.UUID | None = Field(default=None, foreign_key="agent_carts.id", index=True)
    agent_client_id: uuid.UUID | None = Field(default=None, foreign_key="agent_clients.id")
    protocol: AgentProtocol = Field(
        sa_column=Column(pg_enum(AgentProtocol, "agent_protocol"), nullable=False)
    )
    idempotency_key: str
    status: TransactionStatus = Field(
        default=TransactionStatus.PENDING,
        sa_column=Column(pg_enum(TransactionStatus, "transaction_status"), nullable=False),
    )
    payment_provider: str = "stripe"
    # Provider-side reference (e.g. PaymentIntent id). Raw delegated tokens are never stored.
    payment_ref: str | None = None
    amount: Decimal = _money()
    currency: str = Field(default="USD", sa_column=Column(String(3), nullable=False))
    draft_order_gid: str | None = None
    order_gid: str | None = None
    order_name: str | None = None
    order_status_url: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())


class TransactionEvent(SQLModel, table=True):
    """Append-only audit trail for every state change of an agent transaction."""

    __tablename__ = "transaction_events"

    id: int | None = Field(default=None, sa_column=Column(BigInteger, Identity(), primary_key=True))
    transaction_id: uuid.UUID = Field(
        foreign_key="agent_transactions.id", index=True, ondelete="CASCADE"
    )
    event_type: str
    payload: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())


class WebhookEvent(SQLModel, table=True):
    __tablename__ = "webhook_events"

    id: int | None = Field(default=None, sa_column=Column(BigInteger, Identity(), primary_key=True))
    shop_domain: str = Field(index=True)
    topic: str
    webhook_id: str = Field(unique=True)
    payload: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )
    received_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
    processed_at: datetime | None = Field(default=None, sa_column=tz_column(nullable=True))
