"""0044 — xioflow_tasks operational columns: lease, retry, priority.

Problem:
  xioflow_tasks has no lease/retry tracking — if a worker crashes mid-run,
  the task stays RUNNING forever with no recovery path. There is also no
  priority column, so all runs execute FIFO regardless of urgency.

Solution:
  - leased_by: which worker (worker_enrollments.id) claimed this task
  - lease_expires_at: TTL — reaper reverts to PENDING if expired without completion
  - retry_count: how many times this task has been attempted
  - max_retries: ceiling before moving to xioflow_dead_letters
  - priority: higher = claimed first (default 0)

  Two indexes:
  - ix_xioflow_tasks_claimable: for dispatcher (priority DESC then created_at)
  - ix_xioflow_tasks_stale_leases: for reaper (find expired RUNNING tasks)

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Operational columns on xioflow_tasks ────────────────────────────────
    conn.execute(sa.text("""
        ALTER TABLE xioflow_tasks
            ADD COLUMN IF NOT EXISTS leased_by        UUID,
            ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS retry_count      INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS max_retries      INTEGER NOT NULL DEFAULT 3,
            ADD COLUMN IF NOT EXISTS priority         INTEGER NOT NULL DEFAULT 0
    """))

    # ── Index: dispatcher claims highest-priority PENDING tasks first ────────
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_xioflow_tasks_claimable
            ON xioflow_tasks (priority DESC, created_at ASC)
            WHERE state = 'PENDING'
    """))

    # ── Index: reaper finds RUNNING tasks whose lease has expired ───────────
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_xioflow_tasks_stale_leases
            ON xioflow_tasks (lease_expires_at ASC)
            WHERE state = 'RUNNING'
    """))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text(
        "DROP INDEX IF EXISTS ix_xioflow_tasks_stale_leases"
    ))
    conn.execute(sa.text(
        "DROP INDEX IF EXISTS ix_xioflow_tasks_claimable"
    ))
    conn.execute(sa.text("""
        ALTER TABLE xioflow_tasks
            DROP COLUMN IF EXISTS leased_by,
            DROP COLUMN IF EXISTS lease_expires_at,
            DROP COLUMN IF EXISTS retry_count,
            DROP COLUMN IF EXISTS max_retries,
            DROP COLUMN IF EXISTS priority
    """))
