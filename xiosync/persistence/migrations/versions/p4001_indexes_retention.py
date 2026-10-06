"""Phase 4 audit: missing FK indexes + data retention cleanup function

Revision ID: p4001_indexes_retention
Revises: p3001_softdel_ts
Create Date: 2026-10-06 19:50:00.000000+00:00

Addresses audit findings:
  - LOW #28: 24 FK columns missing indexes — slow JOINs and cascading deletes
  - LOW #23: No automated data retention — old runs/dead-letters accumulate

Adds:
  1. Indexes on all 24 unindexed FK columns
  2. xiosync_retention_cleanup() SQL function for scheduled purging
"""
from alembic import op
from sqlalchemy import text

revision = "p4001_indexes_retention"
down_revision = "p3001_softdel_ts"

# FK columns that have no index (discovered by audit query)
_FK_INDEXES = [
    ("actors", "created_by"),
    ("actors", "parent_id"),
    ("artifacts", "created_by"),
    ("bootstrap_tokens", "created_by"),
    ("credentials", "organization_id"),
    ("database_providers", "backup_for_id"),
    ("document_pages", "artifact_id"),
    ("identity_leases", "organization_id"),
    ("memory", "superseded_by"),
    ("mesh_node_bindings", "mesh_node_id"),
    ("mesh_nodes", "network_id"),
    ("operations", "initiated_by"),
    ("operations", "parent_operation_id"),
    ("plugin_installations", "approved_by"),
    ("plugin_installations", "grant_id"),
    ("plugin_installations", "requested_by"),
    ("plugins", "required_capability_id"),
    ("secret_refs", "created_by"),
    ("storage_providers", "backup_for_id"),
    ("type_registry_aliases", "target_id"),
    ("worker_enrollments", "approved_by"),
    ("xioflow_memory_nodes", "recorded_by"),
    ("xioflow_triggers", "template_id"),
    ("xiogrid_pppoe_exit_nodes", "fingerprint_profile_id"),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Add missing FK indexes ─────────────────────────────────────────
    for table, column in _FK_INDEXES:
        idx_name = f"ix_{table}_{column}"
        conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({column})"
        ))

    # ── 2. Data retention cleanup function ────────────────────────────────
    # Callable via: SELECT xiosync_retention_cleanup(30, 90, 180);
    # Parameters: runs_days, dead_letters_days, events_days
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION xiosync_retention_cleanup(
            p_runs_days       INTEGER DEFAULT 90,
            p_dead_letters_days INTEGER DEFAULT 180,
            p_events_days     INTEGER DEFAULT 365
        )
        RETURNS TABLE(
            table_name TEXT,
            rows_deleted BIGINT
        )
        LANGUAGE plpgsql AS $$
        DECLARE
            _count BIGINT;
        BEGIN
            -- Purge completed/failed runs older than p_runs_days
            DELETE FROM xioflow_tasks
            WHERE run_id IN (
                SELECT id FROM xioflow_runs
                WHERE state IN ('SUCCESS', 'FAILED', 'CANCELLED')
                  AND finished_at < now() - make_interval(days => p_runs_days)
            );
            GET DIAGNOSTICS _count = ROW_COUNT;
            table_name := 'xioflow_tasks'; rows_deleted := _count;
            RETURN NEXT;

            DELETE FROM xioflow_runs
            WHERE state IN ('SUCCESS', 'FAILED', 'CANCELLED')
              AND finished_at < now() - make_interval(days => p_runs_days);
            GET DIAGNOSTICS _count = ROW_COUNT;
            table_name := 'xioflow_runs'; rows_deleted := _count;
            RETURN NEXT;

            -- Purge dead letters older than p_dead_letters_days
            DELETE FROM xioflow_dead_letters
            WHERE created_at < now() - make_interval(days => p_dead_letters_days);
            GET DIAGNOSTICS _count = ROW_COUNT;
            table_name := 'xioflow_dead_letters'; rows_deleted := _count;
            RETURN NEXT;

            -- Purge old events
            DELETE FROM events
            WHERE created_at < now() - make_interval(days => p_events_days);
            GET DIAGNOSTICS _count = ROW_COUNT;
            table_name := 'events'; rows_deleted := _count;
            RETURN NEXT;

            -- Purge soft-deleted records older than 30 days
            DELETE FROM actors WHERE deleted_at < now() - INTERVAL '30 days';
            GET DIAGNOSTICS _count = ROW_COUNT;
            table_name := 'actors (soft-deleted)'; rows_deleted := _count;
            RETURN NEXT;

            DELETE FROM identities WHERE deleted_at < now() - INTERVAL '30 days';
            GET DIAGNOSTICS _count = ROW_COUNT;
            table_name := 'identities (soft-deleted)'; rows_deleted := _count;
            RETURN NEXT;
        END;
        $$;
    """))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(text("DROP FUNCTION IF EXISTS xiosync_retention_cleanup"))

    for table, column in _FK_INDEXES:
        idx_name = f"ix_{table}_{column}"
        conn.execute(text(f"DROP INDEX IF EXISTS {idx_name}"))
