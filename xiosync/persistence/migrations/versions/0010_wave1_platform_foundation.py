"""Wave 1 platform foundation. Revision ID: 0010; Revises: 0009.

Gap Analysis v2 — Wave 1 emergency fixes and foundation columns:

* **X-1** — Add ``FORCE ROW LEVEL SECURITY`` to the six tables created in
  revisions 0008 and 0009 that only had ``ENABLE``.  Without ``FORCE``, table
  owners bypass RLS entirely.

* **T-1** — Add ``input`` (JSONB) to ``tasks``.  Workers receive task input
  parameters at lease time instead of looking them up out-of-band.

* **T-4** — Add ``progress`` (JSONB) and ``progress_updated_at`` (TIMESTAMPTZ)
  to ``tasks``.  Workers report incremental progress via heartbeat.

* **X-3** — Add ``priority`` (SMALLINT, 0–10, default 5) to ``tasks`` with a
  descending-priority dispatch index for future priority-aware queue polling.

* **W-4** — Add ``software_version`` (TEXT), ``software_hash`` (TEXT), and
  ``capability_manifest`` (JSONB) to ``worker_enrollments`` for worker fleet
  versioning and capability-based dispatch.

* **D-2** — Add ``external_providers`` (JSONB) to ``organizations`` for
  user-declared storage/database/vector-store provider registry.

Reversibility (INV-MIG-3): downgrade drops all added columns, removes the new
index, and reverts ``FORCE RLS`` with ``NO FORCE``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None

# Tables that need FORCE RLS added (X-1).
_FORCE_RLS_TABLES = (
    "worker_enrollments",
    "worker_credentials",
    "plugins",
    "plugin_rpc_methods",
    "plugin_installations",
    "plugin_network_allow_rules",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # X-1: Add FORCE ROW LEVEL SECURITY to 0008/0009 tables
    # ------------------------------------------------------------------
    for table in _FORCE_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ------------------------------------------------------------------
    # T-1: tasks.input — task input parameters (JSONB, nullable)
    # ------------------------------------------------------------------
    op.add_column(
        "tasks",
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # ------------------------------------------------------------------
    # T-4: tasks.progress + progress_updated_at — incremental progress
    # ------------------------------------------------------------------
    op.add_column(
        "tasks",
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "progress_updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # X-3: tasks.priority — task scheduling priority (0=lowest, 10=highest)
    # ------------------------------------------------------------------
    op.add_column(
        "tasks",
        sa.Column(
            "priority",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("5"),
        ),
    )
    op.create_check_constraint(
        "priority_range",
        "tasks",
        "priority >= 0 AND priority <= 10",
    )
    # Descending-priority index for future dispatch: highest priority first.
    op.create_index(
        "ix_tasks_org_priority_state",
        "tasks",
        [
            "organization_id",
            sa.text("priority DESC"),
            "state",
        ],
    )

    # ------------------------------------------------------------------
    # W-4: worker_enrollments — version tracking & capability manifest
    # ------------------------------------------------------------------
    op.add_column(
        "worker_enrollments",
        sa.Column("software_version", sa.Text(), nullable=True),
    )
    op.add_column(
        "worker_enrollments",
        sa.Column("software_hash", sa.Text(), nullable=True),
    )
    op.add_column(
        "worker_enrollments",
        sa.Column(
            "capability_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # ------------------------------------------------------------------
    # D-2: organizations.external_providers — user-declared provider registry
    # ------------------------------------------------------------------
    op.add_column(
        "organizations",
        sa.Column(
            "external_providers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # D-2
    op.drop_column("organizations", "external_providers")

    # W-4
    op.drop_column("worker_enrollments", "capability_manifest")
    op.drop_column("worker_enrollments", "software_hash")
    op.drop_column("worker_enrollments", "software_version")

    # X-3
    op.drop_index("ix_tasks_org_priority_state", table_name="tasks")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT ck_tasks_priority_range")
    op.drop_column("tasks", "priority")

    # T-4
    op.drop_column("tasks", "progress_updated_at")
    op.drop_column("tasks", "progress")

    # T-1
    op.drop_column("tasks", "input")

    # X-1: Revert FORCE back to NO FORCE
    for table in _FORCE_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
