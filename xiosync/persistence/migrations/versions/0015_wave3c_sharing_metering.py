"""Wave 3C — cross-org sharing and usage metering.

Revision ID: 0015
Revises: 0014

Gap M-2:  ``resource_shares`` table for cross-organization resource sharing.
Gap M-3:  ``usage_meters`` table for billing-ready observability.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels = None
depends_on = None

_ORG_ISOLATION = (
    "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # M-2: resource_shares — cross-organization resource sharing
    # ------------------------------------------------------------------
    op.create_table(
        "resource_shares",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "target_org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=True,  # NULL = public/global share
        ),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[\"read\"]'::jsonb"),
        ),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "expires_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "resource_type IN ('capability', 'artifact', 'workflow', 'plugin')",
            name="ck_resource_shares_type_allowed",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'revoked')",
            name="ck_resource_shares_state_allowed",
        ),
    )
    op.create_index(
        "ix_resource_shares_source_org",
        "resource_shares",
        ["source_org_id"],
    )
    op.create_index(
        "ix_resource_shares_target_org",
        "resource_shares",
        ["target_org_id"],
    )
    op.create_index(
        "ix_resource_shares_resource",
        "resource_shares",
        ["resource_type", "resource_id"],
    )

    # resource_shares does NOT have RLS — it is read by RLS policies on
    # other tables. Access control is at the service layer.

    # ------------------------------------------------------------------
    # M-3: usage_meters — billing-ready observability
    # ------------------------------------------------------------------
    op.create_table(
        "usage_meters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "period_start",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "period_end",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column("metric_type", sa.Text(), nullable=False),
        sa.Column(
            "value",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "organization_id",
            "period_start",
            "metric_type",
            name="uq_usage_meters_org_period_metric",
        ),
    )
    op.create_index(
        "ix_usage_meters_organization_id",
        "usage_meters",
        ["organization_id"],
    )
    op.create_index(
        "ix_usage_meters_org_metric_period",
        "usage_meters",
        ["organization_id", "metric_type", "period_start"],
    )

    # RLS
    op.execute("ALTER TABLE usage_meters ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_meters FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY org_isolation ON usage_meters USING ({_ORG_ISOLATION})"
    )


def downgrade() -> None:
    # M-3
    op.execute("DROP POLICY IF EXISTS org_isolation ON usage_meters")
    op.execute("ALTER TABLE usage_meters NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_meters DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_usage_meters_org_metric_period", table_name="usage_meters")
    op.drop_index("ix_usage_meters_organization_id", table_name="usage_meters")
    op.drop_table("usage_meters")

    # M-2
    op.drop_index("ix_resource_shares_resource", table_name="resource_shares")
    op.drop_index("ix_resource_shares_target_org", table_name="resource_shares")
    op.drop_index("ix_resource_shares_source_org", table_name="resource_shares")
    op.drop_table("resource_shares")
