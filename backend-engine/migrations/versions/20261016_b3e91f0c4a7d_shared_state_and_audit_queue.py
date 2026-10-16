"""shared state and audit queue

Postgres-backed state shared by every replica (service-JWT replay guard, llm.txt cache) and
job-queue columns on audit_runs so audits survive restarts and are retried.

Revision ID: b3e91f0c4a7d
Revises: 7d2f4b8e1c63
Create Date: 2026-10-16 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e91f0c4a7d"
down_revision: str | Sequence[str] | None = "7d2f4b8e1c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ADD VALUE cannot run inside the migration's transaction on older Postgres versions.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE gateway.run_status ADD VALUE IF NOT EXISTS 'queued' BEFORE 'running'"
        )

    op.add_column(
        "audit_runs",
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        schema="gateway",
    )
    op.add_column(
        "audit_runs",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema="gateway",
    )
    op.alter_column("audit_runs", "next_attempt_at", server_default=None, schema="gateway")
    op.add_column(
        "audit_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        schema="gateway",
    )
    op.add_column(
        "audit_runs", sa.Column("last_error", sa.String(), nullable=True), schema="gateway"
    )
    op.create_index(
        "ix_audit_runs_status_next_attempt_at",
        "audit_runs",
        ["status", "next_attempt_at"],
        unique=False,
        schema="gateway",
    )

    op.create_table(
        "service_token_jtis",
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("jti", name=op.f("pk_service_token_jtis")),
        schema="gateway",
    )
    op.create_index(
        op.f("ix_service_token_jtis_expires_at"),
        "service_token_jtis",
        ["expires_at"],
        unique=False,
        schema="gateway",
    )
    op.create_table(
        "llm_txt_cache",
        sa.Column("shop_domain", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("shop_domain", name=op.f("pk_llm_txt_cache")),
        schema="gateway",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("llm_txt_cache", schema="gateway")
    op.drop_index(
        op.f("ix_service_token_jtis_expires_at"), table_name="service_token_jtis", schema="gateway"
    )
    op.drop_table("service_token_jtis", schema="gateway")
    op.drop_index("ix_audit_runs_status_next_attempt_at", table_name="audit_runs", schema="gateway")
    op.drop_column("audit_runs", "last_error", schema="gateway")
    op.drop_column("audit_runs", "heartbeat_at", schema="gateway")
    op.drop_column("audit_runs", "next_attempt_at", schema="gateway")
    op.drop_column("audit_runs", "attempts", schema="gateway")
    # Postgres cannot drop an enum value; fold queued runs back into the old states.
    op.execute("UPDATE gateway.audit_runs SET status = 'failed' WHERE status = 'queued'")
