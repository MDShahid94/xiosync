"""Migration 0029 — XIOFLOW execution tables + workflow_templates schema.

Creates:
  - workflow_templates       (was scaffold in WorkflowTemplate model — now real)
  - xioflow_runs             workflow execution instances
  - xioflow_tasks            individual node step results within a run
  - xioflow_triggers         cron + event trigger definitions
  - xioflow_dead_letters     failed task retry queue

Revision ID: 0029
Revises: 0028_extend_registry_categories
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0029"
down_revision = "0028_extend_registry_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── workflow_templates ──────────────────────────────────────────────────
    # template_type = 'script'      → .mjs script file (script_ref)
    # template_type = 'xioflow_dag' → MemoryGraph DAG (dag_domain + dag_root_intent)
    op.create_table(
        "workflow_templates",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id", pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True, index=True,
        ),
        sa.Column(
            "project_id", pg.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True, index=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("script_ref", sa.Text, nullable=False, server_default=""),
        sa.Column("template_type", sa.Text, nullable=False, server_default="script"),
        sa.Column("dag_domain", sa.Text, nullable=True),
        sa.Column("dag_root_intent", sa.Text, nullable=True),
        sa.Column("category", sa.Text, nullable=True),
        sa.Column("config", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("is_platform_global", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_at", pg.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", pg.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "template_type IN ('script', 'xioflow_dag')",
            name="ck_wt_template_type",
        ),
    )
    op.create_index(
        "ix_workflow_templates_org_slug", "workflow_templates", ["organization_id", "slug"]
    )

    # ── xioflow_runs ────────────────────────────────────────────────────────
    op.create_table(
        "xioflow_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", pg.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "template_id", pg.UUID(as_uuid=True),
            sa.ForeignKey("workflow_templates.id", ondelete="SET NULL"),
            nullable=True, index=True,
        ),
        sa.Column("trigger_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.Text, nullable=False, server_default="PENDING"),
        sa.Column("context", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("result", pg.JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("started_at", pg.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", pg.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED')",
            name="ck_xfr_state",
        ),
    )
    op.create_index("ix_xioflow_runs_org_state", "xioflow_runs", ["organization_id", "state"])

    # ── xioflow_tasks ───────────────────────────────────────────────────────
    op.create_table(
        "xioflow_tasks",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id", pg.UUID(as_uuid=True),
            sa.ForeignKey("xioflow_runs.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("node_intent", sa.Text, nullable=False),
        sa.Column("node_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.Text, nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("result", pg.JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", pg.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("claimed_at", pg.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", pg.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("worker_id", pg.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('PENDING', 'CLAIMED', 'SUCCESS', 'FAILED', 'DEAD')",
            name="ck_xft_state",
        ),
    )
    op.create_index("ix_xioflow_tasks_run_state", "xioflow_tasks", ["run_id", "state"])

    # ── xioflow_triggers ────────────────────────────────────────────────────
    op.create_table(
        "xioflow_triggers",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", pg.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "template_id", pg.UUID(as_uuid=True),
            sa.ForeignKey("workflow_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger_type", sa.Text, nullable=False),
        sa.Column("cron_schedule", sa.Text, nullable=True),
        sa.Column("event_name", sa.Text, nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("context_defaults", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("last_fired_at", pg.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", pg.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "trigger_type IN ('cron', 'event')",
            name="ck_xftr_type",
        ),
    )

    # ── xioflow_dead_letters ────────────────────────────────────────────────
    op.create_table(
        "xioflow_dead_letters",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id", pg.UUID(as_uuid=True),
            sa.ForeignKey("xioflow_runs.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("task_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("resolved", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", pg.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("resolved_at", pg.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_xioflow_dlq_unresolved", "xioflow_dead_letters", ["resolved", "run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_xioflow_dlq_unresolved", "xioflow_dead_letters")
    op.drop_table("xioflow_dead_letters")
    op.drop_table("xioflow_triggers")
    op.drop_index("ix_xioflow_tasks_run_state", "xioflow_tasks")
    op.drop_table("xioflow_tasks")
    op.drop_index("ix_xioflow_runs_org_state", "xioflow_runs")
    op.drop_table("xioflow_runs")
    op.drop_index("ix_workflow_templates_org_slug", "workflow_templates")
    op.drop_table("workflow_templates")
