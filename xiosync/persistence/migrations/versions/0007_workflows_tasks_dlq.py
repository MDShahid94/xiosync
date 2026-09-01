"""Workflow / WorkflowRun / Task / DeadLetter spine. Revision ID: 0007; Revises: 0006.

Creates the Phase 3 durable-execution tables (doc 03 §§2.11, 4.4; doc 06 §5;
doc 07): ``workflows`` (versioned DAG definitions), ``workflow_runs`` (one
execution each), ``tasks`` (leaseable units carrying the lease protocol) and
``dead_letters`` (failed tasks awaiting governed correction).

Structural DDL mirrors the ORM metadata in ``persistence/models/workflows``
(what autogenerate emits and the INV-TEST-SCHEMA-2 diff gate compares against).
RLS org-isolation is applied by hand — autogenerate does not manage it —
following the precedent set by revisions 0004/0005. Two invariants are surfaced
at the schema here and the closed state sets are ``CHECK``-fixed; the ``spec``
DAG contents are *not* CHECK-able and are validated on publish (INV-WF-1) in the
service layer.

Reversibility (INV-MIG-3): downgrade drops the four tables in FK-safe order, so
the empty-DB ``upgrade -> downgrade -> upgrade`` round-trip stays clean.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None

# Standard tenant isolation: a row's org must equal the request's current org
# (the GUC bound by org_scoped_session; mirrors revisions 0004/0005).
_ORG_ISOLATION = "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"

_WORKFLOW_TABLES = ("workflows", "workflow_runs", "tasks", "dead_letters")


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "spec", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("state", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint(
            "organization_id",
            "name",
            "version",
            name="uq_workflows_organization_id_name_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_workflows_created_by_same_org",
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'deprecated')",
            name="state_allowed",
        ),
    )
    op.create_index("ix_workflows_organization_id", "workflows", ["organization_id"])

    op.create_table(
        "workflow_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("initiated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workflow_id"],
            ["workflows.organization_id", "workflows.id"],
            name="fk_workflow_runs_workflow_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "initiated_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_workflow_runs_initiated_by_same_org",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled')",
            name="state_allowed",
        ),
    )
    op.create_index("ix_workflow_runs_organization_id", "workflow_runs", ["organization_id"])
    op.create_index(
        "ix_workflow_runs_org_workflow", "workflow_runs", ["organization_id", "workflow_id"]
    )

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("capability_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True)),
        sa.Column("leased_by", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_expires_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("result", postgresql.JSONB()),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_tasks_run_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "capability_id"],
            ["capabilities.organization_id", "capabilities.id"],
            name="fk_tasks_capability_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "leased_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_tasks_leased_by_same_org",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'leased', 'completed', 'failed', 'expired', 'dead_letter')",
            name="state_allowed",
        ),
    )
    op.create_index("ix_tasks_organization_id", "tasks", ["organization_id"])
    op.create_index(
        "ix_tasks_org_state_lease_expires",
        "tasks",
        ["organization_id", "state", "lease_expires_at"],
    )

    op.create_table(
        "dead_letters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("diagnosis", postgresql.JSONB()),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True)),
        sa.Column("state", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_dead_letters_task_same_org",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'investigating', 'resolved')",
            name="state_allowed",
        ),
    )
    op.create_index("ix_dead_letters_organization_id", "dead_letters", ["organization_id"])
    op.create_index("ix_dead_letters_org_state", "dead_letters", ["organization_id", "state"])

    # Row-Level Security (hand-applied; not autogenerated). All four tables are
    # standard tenant tables: a row is visible/writable only within its org.
    for table in _WORKFLOW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_{table}_org_isolation ON {table} "
            f"USING ({_ORG_ISOLATION}) WITH CHECK ({_ORG_ISOLATION})"
        )


def downgrade() -> None:
    op.drop_table("dead_letters")
    op.drop_table("tasks")
    op.drop_table("workflow_runs")
    op.drop_table("workflows")
