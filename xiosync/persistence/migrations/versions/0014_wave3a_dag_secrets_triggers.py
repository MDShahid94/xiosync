"""Wave 3A — DAG data flow, secrets management, workflow triggers.

Revision ID: 0014
Revises: 0013

Gap S-1:  ``secret_refs`` table for provider-agnostic secrets abstraction.
Gap R-5:  ``workflow_triggers`` table for cron, event, and webhook triggers.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels = None
depends_on = None

_ORG_ISOLATION = (
    "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # S-1: secret_refs — provider-agnostic secrets abstraction
    # ------------------------------------------------------------------
    op.create_table(
        "secret_refs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column(
            "ref_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
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
            "rotated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_secret_refs_org_name"),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_secret_refs_created_by_same_org",
        ),
        sa.CheckConstraint(
            "provider IN ('env', 'vault', 'aws-sm', 'gcp-sm', 'azure-kv', 'inline', 'custom')",
            name="ck_secret_refs_provider_allowed",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'rotated', 'revoked')",
            name="ck_secret_refs_state_allowed",
        ),
    )
    op.create_index(
        "ix_secret_refs_organization_id",
        "secret_refs",
        ["organization_id"],
    )
    op.create_index(
        "ix_secret_refs_org_state",
        "secret_refs",
        ["organization_id", "state"],
    )

    # RLS
    op.execute("ALTER TABLE secret_refs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE secret_refs FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY org_isolation ON secret_refs USING ({_ORG_ISOLATION})"
    )

    # ------------------------------------------------------------------
    # R-5: workflow_triggers — cron, event, and webhook triggers
    # ------------------------------------------------------------------
    op.create_table(
        "workflow_triggers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "last_fired_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "next_fire_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workflow_id"],
            ["workflows.organization_id", "workflows.id"],
            name="fk_workflow_triggers_workflow_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_workflow_triggers_created_by_same_org",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('cron', 'event', 'webhook')",
            name="ck_workflow_triggers_type_allowed",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'disabled')",
            name="ck_workflow_triggers_state_allowed",
        ),
    )
    op.create_index(
        "ix_workflow_triggers_organization_id",
        "workflow_triggers",
        ["organization_id"],
    )
    op.create_index(
        "ix_workflow_triggers_org_state",
        "workflow_triggers",
        ["organization_id", "state"],
    )
    op.create_index(
        "ix_workflow_triggers_next_fire",
        "workflow_triggers",
        ["next_fire_at"],
        postgresql_where=sa.text("state = 'active' AND trigger_type = 'cron'"),
    )

    # RLS
    op.execute("ALTER TABLE workflow_triggers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflow_triggers FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY org_isolation ON workflow_triggers USING ({_ORG_ISOLATION})"
    )


def downgrade() -> None:
    # R-5
    op.execute("DROP POLICY IF EXISTS org_isolation ON workflow_triggers")
    op.execute("ALTER TABLE workflow_triggers NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflow_triggers DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_workflow_triggers_next_fire", table_name="workflow_triggers")
    op.drop_index("ix_workflow_triggers_org_state", table_name="workflow_triggers")
    op.drop_index("ix_workflow_triggers_organization_id", table_name="workflow_triggers")
    op.drop_table("workflow_triggers")

    # S-1
    op.execute("DROP POLICY IF EXISTS org_isolation ON secret_refs")
    op.execute("ALTER TABLE secret_refs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE secret_refs DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_secret_refs_org_state", table_name="secret_refs")
    op.drop_index("ix_secret_refs_organization_id", table_name="secret_refs")
    op.drop_table("secret_refs")
