"""XIOFlow Agent tables — cognitive AI actors for workflow orchestration

Revision ID: 0054_xioflow_agents
Revises: p4001_indexes_retention
Create Date: 2026-10-11 14:00:00.000000+05:30

Creates:
  1. xioflow_agents        — Agent definitions (type, capabilities, config)
  2. xioflow_agent_runs    — Agent execution history (goal → plan → result)

Architecture:
  - Each agent has a dedicated actors row (actor_type='agent') for unified
    RBAC, audit trails, and trust tier governance.
  - Agents are sandboxed within organizations and optionally projects.
  - The capability_ids array restricts what an agent can do (fine-grained).
  - tool_bindings JSONB defines which plugins/integrations are available.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0054_xioflow_agents"
down_revision = "p4001_indexes_retention"


def upgrade() -> None:
    # ── 1. xioflow_agents ─────────────────────────────────────────────────
    op.create_table(
        "xioflow_agents",
        # Identity
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        # Type & RBAC
        sa.Column(
            "agent_type",
            sa.Text,
            nullable=False,
            server_default=sa.text("'browser_automation'"),
        ),
        sa.Column("actor_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "capability_ids",
            pg.ARRAY(pg.UUID(as_uuid=True)),
            server_default=sa.text("'{}'"),
        ),
        # Configuration
        sa.Column(
            "model_config",
            pg.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "tool_bindings",
            pg.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("system_prompt", sa.Text, nullable=True),
        sa.Column(
            "memory_scope",
            sa.Text,
            nullable=False,
            server_default=sa.text("'project'"),
        ),
        # Runtime State
        sa.Column(
            "state",
            sa.Text,
            nullable=False,
            server_default=sa.text("'idle'"),
        ),
        sa.Column(
            "max_concurrent_runs",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "last_run_at",
            pg.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "total_runs",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_successes",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_failures",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Timestamps
        sa.Column(
            "created_at",
            pg.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", pg.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", pg.TIMESTAMP(timezone=True), nullable=True),
    )

    # ── Constraints for xioflow_agents ────────────────────────────────────
    op.create_unique_constraint(
        "uq_xioflow_agents_organization_id_id",
        "xioflow_agents",
        ["organization_id", "id"],
    )
    op.create_unique_constraint(
        "uq_xioflow_agents_org_slug",
        "xioflow_agents",
        ["organization_id", "slug"],
    )

    # Same-org actor FK (INV-AGENT-1: cross-org agent-actor linkage is structurally impossible)
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "ALTER TABLE xioflow_agents ADD CONSTRAINT fk_xioflow_agents_actor_same_org "
            "FOREIGN KEY (organization_id, actor_id) "
            "REFERENCES actors (organization_id, id)"
        )
    )

    # Check constraints
    conn.execute(
        sa.text(
            "ALTER TABLE xioflow_agents ADD CONSTRAINT ck_xfa_agent_type_allowed "
            "CHECK (agent_type IN ("
            "'browser_automation', 'data_extraction', 'system_admin', "
            "'orchestrator', 'custom'))"
        )
    )
    conn.execute(
        sa.text(
            "ALTER TABLE xioflow_agents ADD CONSTRAINT ck_xfa_memory_scope_allowed "
            "CHECK (memory_scope IN ('project', 'organization', 'global'))"
        )
    )
    conn.execute(
        sa.text(
            "ALTER TABLE xioflow_agents ADD CONSTRAINT ck_xfa_state_allowed "
            "CHECK (state IN ("
            "'idle', 'planning', 'executing', 'waiting_human', "
            "'errored', 'suspended', 'archived'))"
        )
    )
    conn.execute(
        sa.text(
            "ALTER TABLE xioflow_agents ADD CONSTRAINT ck_xfa_max_concurrent_positive "
            "CHECK (max_concurrent_runs > 0)"
        )
    )

    # Indexes
    op.create_index(
        "ix_xioflow_agents_org_type_state",
        "xioflow_agents",
        ["organization_id", "agent_type", "state"],
    )
    op.create_index(
        "ix_xioflow_agents_actor_id",
        "xioflow_agents",
        ["actor_id"],
    )

    # ── 2. xioflow_agent_runs ─────────────────────────────────────────────
    op.create_table(
        "xioflow_agent_runs",
        # Identity
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "agent_id",
            pg.UUID(as_uuid=True),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Goal & Plan
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column(
            "plan",
            pg.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "plan_steps_total",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "plan_steps_completed",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Execution State
        sa.Column(
            "state",
            sa.Text,
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column(
            "priority",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "dag_run_ids",
            pg.ARRAY(pg.UUID(as_uuid=True)),
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "context",
            pg.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result",
            pg.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error", sa.Text, nullable=True),
        # Timestamps
        sa.Column(
            "started_at",
            pg.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", pg.TIMESTAMP(timezone=True), nullable=True),
    )

    # ── Constraints for xioflow_agent_runs ────────────────────────────────
    # Same-org agent FK
    conn.execute(
        sa.text(
            "ALTER TABLE xioflow_agent_runs ADD CONSTRAINT fk_xioflow_agent_runs_agent_same_org "
            "FOREIGN KEY (organization_id, agent_id) "
            "REFERENCES xioflow_agents (organization_id, id)"
        )
    )

    # State check constraint
    conn.execute(
        sa.text(
            "ALTER TABLE xioflow_agent_runs ADD CONSTRAINT ck_xfar_state_allowed "
            "CHECK (state IN ("
            "'PENDING', 'PLANNING', 'EXECUTING', 'WAITING_HUMAN', "
            "'SUCCESS', 'FAILED', 'CANCELLED'))"
        )
    )

    # Indexes
    op.create_index(
        "ix_xioflow_agent_runs_agent_state",
        "xioflow_agent_runs",
        ["agent_id", "state"],
    )
    op.create_index(
        "ix_xioflow_agent_runs_org_started",
        "xioflow_agent_runs",
        ["organization_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_table("xioflow_agent_runs")
    op.drop_table("xioflow_agents")
