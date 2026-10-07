"""Phase 1 audit: add organization_id to unscoped tables + FK constraints

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-10-06 10:00:00.000000+00:00

Addresses audit findings:
  - CRITICAL: xioflow_tasks, xioflow_dead_letters missing organization_id
  - CRITICAL: xiogrid_fingerprint_profiles, xiogrid_pppoe_exit_nodes,
              xiogrid_pppoe_hosts missing organization_id
  - CRITICAL: xioflow_runs, xioflow_triggers have organization_id but no FK
  - HIGH: Missing indexes on organization_id for query performance

Strategy:
  1. Add organization_id (nullable initially) to unscoped tables
  2. Backfill from parent tables (xioflow_tasks.run_id → xioflow_runs.organization_id)
  3. For xiogrid tables with no parent reference, backfill with the single existing org
  4. Set NOT NULL after backfill
  5. Add FK constraints + indexes
"""

from alembic import op
from sqlalchemy import text

revision = "f1a2b3c4d5e6"
down_revision = "e5f6a7b8c9d0"


def upgrade() -> None:
    conn = op.get_bind()

    # ── Get the existing organization ID for backfill ─────────────────────
    # In single-org deployments there is exactly one row. Multi-org
    # deployments will have empty unscoped tables (no backfill needed).
    org_row = conn.execute(text("SELECT id FROM organizations LIMIT 1")).fetchone()
    default_org = str(org_row[0]) if org_row else None

    # ── 1. xioflow_tasks — add organization_id, backfill from xioflow_runs ─
    conn.execute(text("ALTER TABLE xioflow_tasks ADD COLUMN IF NOT EXISTS organization_id UUID"))
    conn.execute(
        text("""
        UPDATE xioflow_tasks t
        SET organization_id = r.organization_id
        FROM xioflow_runs r
        WHERE t.run_id = r.id
          AND t.organization_id IS NULL
    """)
    )
    # Rows with no matching run get the default org
    if default_org:
        conn.execute(
            text("UPDATE xioflow_tasks SET organization_id = :org WHERE organization_id IS NULL"),
            {"org": default_org},
        )
    conn.execute(text("ALTER TABLE xioflow_tasks ALTER COLUMN organization_id SET NOT NULL"))

    # ── 2. xioflow_dead_letters — add organization_id, backfill from runs ─
    conn.execute(
        text("ALTER TABLE xioflow_dead_letters ADD COLUMN IF NOT EXISTS organization_id UUID")
    )
    conn.execute(
        text("""
        UPDATE xioflow_dead_letters dl
        SET organization_id = r.organization_id
        FROM xioflow_runs r
        WHERE dl.run_id = r.id
          AND dl.organization_id IS NULL
    """)
    )
    if default_org:
        conn.execute(
            text(
                "UPDATE xioflow_dead_letters SET organization_id = :org "
                "WHERE organization_id IS NULL"
            ),
            {"org": default_org},
        )
    conn.execute(text("ALTER TABLE xioflow_dead_letters ALTER COLUMN organization_id SET NOT NULL"))

    # ── 3. xiogrid_fingerprint_profiles — add organization_id ─────────────
    conn.execute(
        text(
            "ALTER TABLE xiogrid_fingerprint_profiles ADD COLUMN IF NOT EXISTS organization_id UUID"
        )
    )
    if default_org:
        conn.execute(
            text(
                "UPDATE xiogrid_fingerprint_profiles SET organization_id = :org "
                "WHERE organization_id IS NULL"
            ),
            {"org": default_org},
        )
    conn.execute(
        text("ALTER TABLE xiogrid_fingerprint_profiles ALTER COLUMN organization_id SET NOT NULL")
    )

    # ── 4. xiogrid_pppoe_exit_nodes — add organization_id ─────────────────
    conn.execute(
        text("ALTER TABLE xiogrid_pppoe_exit_nodes ADD COLUMN IF NOT EXISTS organization_id UUID")
    )
    if default_org:
        conn.execute(
            text(
                "UPDATE xiogrid_pppoe_exit_nodes SET organization_id = :org "
                "WHERE organization_id IS NULL"
            ),
            {"org": default_org},
        )
    conn.execute(
        text("ALTER TABLE xiogrid_pppoe_exit_nodes ALTER COLUMN organization_id SET NOT NULL")
    )

    # ── 5. xiogrid_pppoe_hosts — add organization_id ──────────────────────
    conn.execute(
        text("ALTER TABLE xiogrid_pppoe_hosts ADD COLUMN IF NOT EXISTS organization_id UUID")
    )
    if default_org:
        conn.execute(
            text(
                "UPDATE xiogrid_pppoe_hosts SET organization_id = :org "
                "WHERE organization_id IS NULL"
            ),
            {"org": default_org},
        )
    conn.execute(text("ALTER TABLE xiogrid_pppoe_hosts ALTER COLUMN organization_id SET NOT NULL"))

    # ── 6. Add FK constraints on organization_id ──────────────────────────
    # Tables that already had org_id but no FK
    _fk_targets = [
        "xioflow_runs",
        "xioflow_triggers",
        # Newly added org_id tables
        "xioflow_tasks",
        "xioflow_dead_letters",
        "xiogrid_fingerprint_profiles",
        "xiogrid_pppoe_exit_nodes",
        "xiogrid_pppoe_hosts",
    ]
    for table in _fk_targets:
        fk_name = f"fk_{table}_org_id"
        # Idempotent: drop if exists, then create
        conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}"))
        conn.execute(
            text(
                f"ALTER TABLE {table} "
                f"ADD CONSTRAINT {fk_name} "
                f"FOREIGN KEY (organization_id) REFERENCES organizations(id) "
                f"ON DELETE RESTRICT"
            )
        )

    # ── 7. Add indexes on organization_id for query performance ───────────
    _idx_targets = [
        "xioflow_tasks",
        "xioflow_dead_letters",
        "xiogrid_fingerprint_profiles",
        "xiogrid_pppoe_exit_nodes",
        "xiogrid_pppoe_hosts",
        # Also add indexes for tables that had org_id but maybe no index
        "xioflow_runs",
        "xioflow_triggers",
    ]
    for table in _idx_targets:
        idx_name = f"ix_{table}_organization_id"
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} (organization_id)"))


def downgrade() -> None:
    conn = op.get_bind()

    # Drop FK constraints
    for table in [
        "xioflow_runs",
        "xioflow_triggers",
        "xioflow_tasks",
        "xioflow_dead_letters",
        "xiogrid_fingerprint_profiles",
        "xiogrid_pppoe_exit_nodes",
        "xiogrid_pppoe_hosts",
    ]:
        conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_org_id"))

    # Drop indexes
    for table in [
        "xioflow_tasks",
        "xioflow_dead_letters",
        "xiogrid_fingerprint_profiles",
        "xiogrid_pppoe_exit_nodes",
        "xiogrid_pppoe_hosts",
        "xioflow_runs",
        "xioflow_triggers",
    ]:
        conn.execute(text(f"DROP INDEX IF EXISTS ix_{table}_organization_id"))

    # Drop organization_id columns from newly-added tables
    for table in [
        "xioflow_tasks",
        "xioflow_dead_letters",
        "xiogrid_fingerprint_profiles",
        "xiogrid_pppoe_exit_nodes",
        "xiogrid_pppoe_hosts",
    ]:
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS organization_id"))
