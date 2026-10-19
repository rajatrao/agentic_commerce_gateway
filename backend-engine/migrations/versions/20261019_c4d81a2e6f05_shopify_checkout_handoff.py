"""shopify checkout handoff

Buyers now pay on the merchant's own Shopify checkout: carts get a `checkout_url` (cart
permalink) and the in-app payment tables are dropped.

Revision ID: c4d81a2e6f05
Revises: b3e91f0c4a7d
Create Date: 2026-10-19 22:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4d81a2e6f05"
down_revision: str | Sequence[str] | None = "b3e91f0c4a7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agent_carts", sa.Column("checkout_url", sa.Text(), nullable=True), schema="gateway"
    )
    op.drop_table("transaction_events", schema="gateway")
    op.drop_table("agent_transactions", schema="gateway")
    op.execute("DROP TYPE IF EXISTS gateway.transaction_status")


def downgrade() -> None:
    """Downgrade schema: recreate the (empty) payment tables."""
    protocol = postgresql.ENUM(name="agent_protocol", schema="gateway", create_type=False)
    status = postgresql.ENUM(
        "pending",
        "authorized",
        "captured",
        "order_created",
        "failed",
        "refunded",
        name="transaction_status",
        schema="gateway",
    )
    op.create_table(
        "agent_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("cart_id", sa.Uuid(), nullable=True),
        sa.Column("agent_client_id", sa.Uuid(), nullable=True),
        sa.Column("protocol", protocol, nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("payment_provider", sa.String(), nullable=False),
        sa.Column("payment_ref", sa.String(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("draft_order_gid", sa.String(), nullable=True),
        sa.Column("order_gid", sa.String(), nullable=True),
        sa.Column("order_name", sa.String(), nullable=True),
        sa.Column("order_status_url", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_detail", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_client_id"],
            ["gateway.agent_clients.id"],
            name=op.f("fk_agent_transactions_agent_client_id_agent_clients"),
        ),
        sa.ForeignKeyConstraint(
            ["cart_id"],
            ["gateway.agent_carts.id"],
            name=op.f("fk_agent_transactions_cart_id_agent_carts"),
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["gateway.merchants.id"],
            name=op.f("fk_agent_transactions_merchant_id_merchants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_transactions")),
        sa.UniqueConstraint(
            "merchant_id",
            "idempotency_key",
            name=op.f("uq_agent_transactions_merchant_id_idempotency_key"),
        ),
        schema="gateway",
    )
    op.create_index(
        op.f("ix_agent_transactions_merchant_id"),
        "agent_transactions",
        ["merchant_id"],
        schema="gateway",
    )
    op.create_index(
        op.f("ix_agent_transactions_cart_id"), "agent_transactions", ["cart_id"], schema="gateway"
    )
    op.create_table(
        "transaction_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["gateway.agent_transactions.id"],
            name=op.f("fk_transaction_events_transaction_id_agent_transactions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transaction_events")),
        schema="gateway",
    )
    op.create_index(
        op.f("ix_transaction_events_transaction_id"),
        "transaction_events",
        ["transaction_id"],
        schema="gateway",
    )
    op.drop_column("agent_carts", "checkout_url", schema="gateway")
