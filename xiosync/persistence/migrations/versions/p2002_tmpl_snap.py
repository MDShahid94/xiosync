"""Phase 2 audit: add template_snapshot for workflow versioning

Revision ID: p2002_tmpl_snap
Revises: p2001_statemachine
Create Date: 2026-10-06 13:15:00.000000+00:00

Addresses audit finding:
  - HIGH: No workflow versioning — templates modified in-place, mid-run
          mutation risk. Workers should use a snapshot captured at dispatch.

Adds template_snapshot JSONB column to xioflow_runs that stores
{slug, dag_domain, dag_root_intent, config, template_type} at dispatch
time. Workers use this snapshot instead of querying the live template.
"""

from alembic import op
from sqlalchemy import text

revision = "p2002_tmpl_snap"
down_revision = "p2001_statemachine"


def upgrade() -> None:
    conn = op.get_bind()

    # Add the snapshot column (nullable — old runs won't have it)
    conn.execute(text("ALTER TABLE xioflow_runs ADD COLUMN IF NOT EXISTS template_snapshot JSONB"))

    # Backfill existing runs from their current template
    conn.execute(
        text("""
        UPDATE xioflow_runs r
        SET template_snapshot = jsonb_build_object(
            'slug', t.slug,
            'dag_domain', t.dag_domain,
            'dag_root_intent', t.dag_root_intent,
            'template_type', t.template_type,
            'config', COALESCE(t.config, '{}'::jsonb)
        )
        FROM workflow_templates t
        WHERE r.template_id = t.id
          AND r.template_snapshot IS NULL
    """)
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("ALTER TABLE xioflow_runs DROP COLUMN IF EXISTS template_snapshot"))
