"""Wave 2C — Real-time, DLQ context, and interop.

Revision ID: 0013
Revises: 0012

Gap T-5:  Rich DLQ context (stack_trace, last_checkpoint, original_input, metadata)
          on ``dead_letters``.
Gap R-2:  Outbound webhook subscriptions table (``webhook_subscriptions``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels = None
depends_on = None

_ORG_ISOLATION = (
    "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # T-5: dead_letters — rich failure context columns
    # ------------------------------------------------------------------
    op.add_column(
        "dead_letters",
        sa.Column(
            "stack_trace",
            sa.Text(),
            nullable=True,
        ),
    )
    op.add_column(
        "dead_letters",
        sa.Column(
            "last_checkpoint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "dead_letters",
        sa.Column(
            "original_input",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "dead_letters",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "dead_letters",
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # ------------------------------------------------------------------
    # R-2: webhook_subscriptions table
    # ------------------------------------------------------------------
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("event_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("signing_secret", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'disabled')",
            name="ck_webhook_subs_state_allowed",
        ),
    )
    op.create_index(
        "ix_webhook_subscriptions_organization_id",
        "webhook_subscriptions",
        ["organization_id"],
    )
    op.create_index(
        "ix_webhook_subs_org_state",
        "webhook_subscriptions",
        ["organization_id", "state"],
    )

    # RLS
    op.execute("ALTER TABLE webhook_subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webhook_subscriptions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY org_isolation ON webhook_subscriptions USING ({_ORG_ISOLATION})"
    )


def downgrade() -> None:
    # R-2
    op.execute("DROP POLICY IF EXISTS org_isolation ON webhook_subscriptions")
    op.execute("ALTER TABLE webhook_subscriptions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webhook_subscriptions DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_webhook_subs_org_state", table_name="webhook_subscriptions")
    op.drop_index("ix_webhook_subscriptions_organization_id", table_name="webhook_subscriptions")
    op.drop_table("webhook_subscriptions")

    # T-5
    op.drop_column("dead_letters", "attempts")
    op.drop_column("dead_letters", "metadata")
    op.drop_column("dead_letters", "original_input")
    op.drop_column("dead_letters", "last_checkpoint")
    op.drop_column("dead_letters", "stack_trace")
