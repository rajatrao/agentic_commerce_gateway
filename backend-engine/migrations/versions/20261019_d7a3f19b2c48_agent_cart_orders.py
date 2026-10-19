"""agent cart orders

Link agent carts to the Shopify order the buyer placed from their checkout link.

Revision ID: d7a3f19b2c48
Revises: c4d81a2e6f05
Create Date: 2026-10-19 22:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a3f19b2c48"
down_revision: str | Sequence[str] | None = "c4d81a2e6f05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    for name in ("order_gid", "order_name", "order_status_url"):
        op.add_column("agent_carts", sa.Column(name, sa.Text(), nullable=True), schema="gateway")
    op.add_column(
        "agent_carts",
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=True),
        schema="gateway",
    )


def downgrade() -> None:
    """Downgrade schema."""
    for name in ("ordered_at", "order_status_url", "order_name", "order_gid"):
        op.drop_column("agent_carts", name, schema="gateway")
