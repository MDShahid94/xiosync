"""Wave 2B — API surface, worker protocols, and tenant quotas.

Revision ID: 0012
Revises: 0011

Gap W-3:  Task checkpoint columns (checkpoint, checkpoint_at) on ``tasks``.
Gap D-3:  Artifact references column (artifact_refs) on ``memory``.
Gap S-2:  Worker network allowlist table (worker_network_allow_rules).
Gap M-1:  Organization resource quotas column (resource_quotas).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None

_ORG_ISOLATION = (
    "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # W-3: tasks — checkpoint columns for state recovery
    # ------------------------------------------------------------------
    op.add_column(
        "tasks",
        sa.Column(
            "checkpoint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "checkpoint_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # D-3: memory — artifact references
    # ------------------------------------------------------------------
    op.add_column(
        "memory",
        sa.Column(
            "artifact_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # ------------------------------------------------------------------
    # M-1: organizations — resource quotas
    # ------------------------------------------------------------------
    op.add_column(
        "organizations",
        sa.Column(
            "resource_quotas",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # ------------------------------------------------------------------
    # S-2: worker_network_allow_rules table
    # ------------------------------------------------------------------
    op.create_table(
        "worker_network_allow_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host_pattern", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column(
            "protocol",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'https'"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "enrollment_id"],
            ["worker_enrollments.organization_id", "worker_enrollments.id"],
            name="fk_worker_net_rules_enrollment_same_org",
        ),
        sa.CheckConstraint(
            "port > 0 AND port <= 65535",
            name="ck_worker_net_rules_port_range",
        ),
        sa.CheckConstraint(
            "protocol IN ('http', 'https', 'wss')",
            name="ck_worker_net_rules_protocol_allowed",
        ),
    )
    op.create_index(
        "ix_worker_network_allow_rules_organization_id",
        "worker_network_allow_rules",
        ["organization_id"],
    )
    op.create_index(
        "ix_worker_net_rules_org_enrollment",
        "worker_network_allow_rules",
        ["organization_id", "enrollment_id"],
    )

    # RLS
    op.execute("ALTER TABLE worker_network_allow_rules ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE worker_network_allow_rules FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY org_isolation ON worker_network_allow_rules USING ({_ORG_ISOLATION})"
    )


def downgrade() -> None:
    # S-2
    op.execute("DROP POLICY IF EXISTS org_isolation ON worker_network_allow_rules")
    op.execute("ALTER TABLE worker_network_allow_rules NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE worker_network_allow_rules DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_worker_net_rules_org_enrollment", table_name="worker_network_allow_rules")
    op.drop_index("ix_worker_network_allow_rules_organization_id", table_name="worker_network_allow_rules")
    op.drop_table("worker_network_allow_rules")

    # M-1
    op.drop_column("organizations", "resource_quotas")

    # D-3
    op.drop_column("memory", "artifact_refs")

    # W-3
    op.drop_column("tasks", "checkpoint_at")
    op.drop_column("tasks", "checkpoint")
