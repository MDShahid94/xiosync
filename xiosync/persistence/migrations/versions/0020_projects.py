"""0020 — projects table + project_id scoping on all XIOBR tables.

Revision ID: 0020
Revises: 0019

This migration:
  1. Creates the ``projects`` table with RLS.
  2. Adds a nullable ``project_id`` column to all six XIOBR tables created in
     0019 (``compute_runtimes``, ``runtime_nodes``, ``browser_pools``,
     ``browser_sessions``, ``mesh_networks``, ``mesh_nodes``).
  3. Adds FK constraints from each ``project_id`` → ``projects.id``.

``project_id`` is nullable so all pre-existing rows remain valid without a
data migration (they simply belong to no project yet).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_ts = TIMESTAMP(timezone=True)

_XIOBR_TABLES = [
    "compute_runtimes",
    "runtime_nodes",
    "browser_pools",
    "browser_sessions",
    "mesh_networks",
]


def upgrade() -> None:
    # 1. Create the projects table
    op.create_table(
        "projects",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("config", JSONB),
        sa.Column("state", sa.Text, nullable=False, server_default="active"),
        sa.Column(
            "created_at", _ts, nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("updated_at", _ts),
        sa.CheckConstraint(
            "state IN ('active', 'archived')", name="ck_projects_state"
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_projects_org_id"),
        sa.UniqueConstraint(
            "organization_id", "slug", name="uq_projects_org_slug"
        ),
        sa.UniqueConstraint(
            "organization_id", "name", name="uq_projects_org_name"
        ),
    )

    # Enable RLS on projects
    op.execute("ALTER TABLE projects ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_projects ON projects
        USING (organization_id = current_setting('app.current_org_id')::uuid)
        """
    )

    # 2. Add project_id column to all XIOBR tables (IF NOT EXISTS for idempotency
    # — subagents may have already added these columns when applying 0019).
    for table in _XIOBR_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            f"project_id UUID REFERENCES projects(id) ON DELETE SET NULL"
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_project_id ON {table}(project_id)"
        )


def downgrade() -> None:
    for table in reversed(_XIOBR_TABLES):
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_project_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS project_id")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_projects ON projects")
    op.drop_table("projects")
