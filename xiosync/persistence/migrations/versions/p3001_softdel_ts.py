"""Phase 3 audit: soft-delete, audit timestamps, bootstrap token revocation

Revision ID: p3001_softdel_ts
Revises: p2002_tmpl_snap
Create Date: 2026-10-06 13:30:00.000000+00:00

Addresses audit findings:
  - MEDIUM: No soft-delete pattern — hard deletes lose audit trail
  - MEDIUM: Missing created_at/updated_at on several tables
  - MEDIUM: No bootstrap token revocation endpoint (schema support)

Adds:
  1. deleted_at TIMESTAMP column to core entity tables (for soft-delete)
  2. created_at/updated_at to tables missing them
  3. bootstrap_tokens table for trackable, revocable tokens
  4. auto-update trigger for updated_at columns
"""
from alembic import op
from sqlalchemy import text

revision = "p3001_softdel_ts"
down_revision = "p2002_tmpl_snap"

# Core entity tables that benefit most from soft-delete
_SOFT_DELETE_TABLES = [
    "actors", "identities", "projects", "plugins",
    "workflow_templates", "worker_enrollments", "credentials",
    "browser_sessions", "browser_pools", "mesh_networks",
    "xioflow_memory_nodes",
]

# Tables missing created_at
_NEEDS_CREATED_AT = [
    "identity_leases", "worker_credentials", "workflow_network_scopes",
    "xiogrid_account_ip_bindings", "xiogrid_fingerprint_profiles",
    "xiogrid_pppoe_exit_nodes", "xiogrid_pppoe_hosts",
]

# xioflow_runs has started_at but not created_at — add it as an alias
# Tables missing updated_at (subset — most critical ones)
_NEEDS_UPDATED_AT = [
    "xioflow_runs", "xioflow_tasks", "xioflow_triggers",
    "xioflow_dead_letters", "xiogrid_fingerprint_profiles",
    "xiogrid_pppoe_exit_nodes", "xiogrid_pppoe_hosts",
    "xiogrid_account_ip_bindings",
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Add deleted_at to core entity tables ───────────────────────────
    for table in _SOFT_DELETE_TABLES:
        conn.execute(text(
            f"ALTER TABLE {table} "
            f"ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ"
        ))
        # Partial index — most queries filter WHERE deleted_at IS NULL
        conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_deleted_at "
            f"ON {table} (deleted_at) WHERE deleted_at IS NOT NULL"
        ))

    # ── 2. Add missing created_at columns ─────────────────────────────────
    for table in _NEEDS_CREATED_AT:
        conn.execute(text(
            f"ALTER TABLE {table} "
            f"ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now()"
        ))

    # ── 3. Add missing updated_at columns ─────────────────────────────────
    for table in _NEEDS_UPDATED_AT:
        conn.execute(text(
            f"ALTER TABLE {table} "
            f"ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()"
        ))

    # ── 4. Auto-update trigger for updated_at ─────────────────────────────
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION trg_set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """))

    _all_updated = set(_NEEDS_UPDATED_AT + _SOFT_DELETE_TABLES)
    for table in _all_updated:
        # Only add trigger if table has updated_at
        has_col = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'updated_at'"
        ), {"t": table}).fetchone()
        if has_col:
            conn.execute(text(
                f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}"
            ))
            conn.execute(text(
                f"CREATE TRIGGER trg_{table}_updated_at "
                f"BEFORE UPDATE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()"
            ))

    # ── 5. Bootstrap tokens table (for revocation) ────────────────────────
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS bootstrap_tokens (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            token_hash    TEXT NOT NULL UNIQUE,
            label         TEXT,
            organization_id UUID NOT NULL REFERENCES organizations(id),
            created_by    UUID REFERENCES actors(id),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ,
            revoked_at    TIMESTAMPTZ,
            last_used_at  TIMESTAMPTZ,
            use_count     INTEGER NOT NULL DEFAULT 0,
            max_uses      INTEGER
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_bootstrap_tokens_org "
        "ON bootstrap_tokens (organization_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_bootstrap_tokens_hash "
        "ON bootstrap_tokens (token_hash)"
    ))


def downgrade() -> None:
    conn = op.get_bind()

    # Drop bootstrap_tokens
    conn.execute(text("DROP TABLE IF EXISTS bootstrap_tokens"))

    # Drop updated_at triggers
    _all_updated = set(_NEEDS_UPDATED_AT + _SOFT_DELETE_TABLES)
    for table in _all_updated:
        conn.execute(text(
            f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}"
        ))
    conn.execute(text("DROP FUNCTION IF EXISTS trg_set_updated_at()"))

    # Drop updated_at columns
    for table in _NEEDS_UPDATED_AT:
        conn.execute(text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS updated_at"
        ))

    # Drop created_at columns
    for table in _NEEDS_CREATED_AT:
        conn.execute(text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS created_at"
        ))

    # Drop deleted_at columns + indexes
    for table in _SOFT_DELETE_TABLES:
        conn.execute(text(
            f"DROP INDEX IF EXISTS ix_{table}_deleted_at"
        ))
        conn.execute(text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS deleted_at"
        ))
