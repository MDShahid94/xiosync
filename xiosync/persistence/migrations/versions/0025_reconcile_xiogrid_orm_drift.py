"""reconcile_xiogrid_orm_drift

Revision ID: b1a06b23332a
Revises: 0024
Create Date: 2026-09-03 15:18:22.611543

Reversibility (INV-MIG-3): reversible unless explicitly documented otherwise
below, with the reason.
"""

from __future__ import annotations

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = '0025'
down_revision: str | None = '0024'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # All DDL uses IF EXISTS / IF NOT EXISTS to be idempotent on both fresh
    # databases (running full migration chain) and existing databases that may
    # have auto-generated FK names from earlier migration runs.

    # Step 1: Formal UNIQUE constraints backing composite FKs.
    # First drop any composite FKs that may already reference the raw indexes
    # from a previous partial migration run — we'll re-add them in Steps 3 & 7.
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_node_same_org")
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_pool_same_org")
    op.execute("ALTER TABLE runtime_nodes DROP CONSTRAINT IF EXISTS fk_runtime_nodes_runtime_same_org")

    # Now convert raw indexes → formal UNIQUE constraints (idempotent).
    for _tbl, _name in [
        ("runtime_nodes", "uq_runtime_nodes_org_id"),
        ("browser_pools", "uq_browser_pools_org_id"),
        ("compute_runtimes", "uq_compute_runtimes_org_id"),
        ("browser_sessions", "uq_browser_sessions_org_id"),
    ]:
        op.execute(f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{_name}') THEN
                    RETURN;  -- Already a formal constraint, skip.
                END IF;
                IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = '{_name}') THEN
                    EXECUTE 'DROP INDEX {_name}';
                END IF;
                EXECUTE 'ALTER TABLE {_tbl} ADD CONSTRAINT {_name} UNIQUE (organization_id, id)';
            END $$;
        """)

    # Step 2: browser_pools — drop old simple FK, add named FK.
    op.execute("ALTER TABLE browser_pools DROP CONSTRAINT IF EXISTS browser_pools_project_id_fkey")
    op.execute("ALTER TABLE browser_pools DROP CONSTRAINT IF EXISTS fk_browser_pools_project_id_projects")
    op.execute("ALTER TABLE browser_pools ADD CONSTRAINT fk_browser_pools_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id)")

    # Step 3: browser_sessions — upgrade to composite same-org FKs.
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_node_id_runtime_nodes")
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS browser_sessions_project_id_fkey")
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_pool_id_browser_pools")
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_node_same_org")
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_project_id_projects")
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_pool_same_org")
    op.execute("ALTER TABLE browser_sessions ADD CONSTRAINT fk_browser_sessions_node_same_org FOREIGN KEY (organization_id, node_id) REFERENCES runtime_nodes (organization_id, id)")
    op.execute("ALTER TABLE browser_sessions ADD CONSTRAINT fk_browser_sessions_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id)")
    op.execute("ALTER TABLE browser_sessions ADD CONSTRAINT fk_browser_sessions_pool_same_org FOREIGN KEY (organization_id, pool_id) REFERENCES browser_pools (organization_id, id) ON DELETE CASCADE")

    # Step 4: compute_runtimes — named FK.
    op.execute("ALTER TABLE compute_runtimes DROP CONSTRAINT IF EXISTS compute_runtimes_project_id_fkey")
    op.execute("ALTER TABLE compute_runtimes DROP CONSTRAINT IF EXISTS fk_compute_runtimes_project_id_projects")
    op.execute("ALTER TABLE compute_runtimes ADD CONSTRAINT fk_compute_runtimes_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id)")

    # Step 5: mesh tables — named FKs.
    op.execute("ALTER TABLE mesh_networks DROP CONSTRAINT IF EXISTS mesh_networks_project_id_fkey")
    op.execute("ALTER TABLE mesh_networks DROP CONSTRAINT IF EXISTS fk_mesh_networks_project_id_projects")
    op.execute("ALTER TABLE mesh_networks ADD CONSTRAINT fk_mesh_networks_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id)")
    op.execute("ALTER TABLE mesh_nodes DROP CONSTRAINT IF EXISTS fk_mesh_nodes_project_id_projects")
    op.execute("ALTER TABLE mesh_nodes ADD CONSTRAINT fk_mesh_nodes_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id)")

    # Step 6: projects — rename UQ constraints to Alembic canonical names.
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS uq_projects_org_id")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS uq_projects_org_name")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS uq_projects_org_slug")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS uq_projects_organization_id_id")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS uq_projects_organization_id_name")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS uq_projects_organization_id_slug")
    op.execute("ALTER TABLE projects ADD CONSTRAINT uq_projects_organization_id_id UNIQUE (organization_id, id)")
    op.execute("ALTER TABLE projects ADD CONSTRAINT uq_projects_organization_id_name UNIQUE (organization_id, name)")
    op.execute("ALTER TABLE projects ADD CONSTRAINT uq_projects_organization_id_slug UNIQUE (organization_id, slug)")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS fk_projects_organization_id_organizations")
    op.execute("ALTER TABLE projects ADD CONSTRAINT fk_projects_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES organizations (id)")

    # Step 7: runtime_nodes — composite same-org FK to compute_runtimes.
    op.execute("ALTER TABLE runtime_nodes DROP CONSTRAINT IF EXISTS fk_runtime_nodes_runtime_id_compute_runtimes")
    op.execute("ALTER TABLE runtime_nodes DROP CONSTRAINT IF EXISTS runtime_nodes_project_id_fkey")
    op.execute("ALTER TABLE runtime_nodes DROP CONSTRAINT IF EXISTS fk_runtime_nodes_runtime_same_org")
    op.execute("ALTER TABLE runtime_nodes DROP CONSTRAINT IF EXISTS fk_runtime_nodes_project_id_projects")
    op.execute("ALTER TABLE runtime_nodes ADD CONSTRAINT fk_runtime_nodes_runtime_same_org FOREIGN KEY (organization_id, runtime_id) REFERENCES compute_runtimes (organization_id, id) ON DELETE CASCADE")
    op.execute("ALTER TABLE runtime_nodes ADD CONSTRAINT fk_runtime_nodes_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id)")

    # Step 8: FORCE ROW LEVEL SECURITY on all tenant tables that have RLS enabled.
    # FORCE RLS ensures even the DB superuser role is subject to policies, which
    # closes the gap caught by test_all_tenant_tables_have_force_rls.
    _force_rls_tables = [
        "browser_pools", "browser_sessions", "compute_runtimes",
        "document_collections", "document_pages",
        "mesh_networks", "mesh_nodes", "projects",
        "resource_shares", "runtime_nodes",
    ]
    for table in _force_rls_tables:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Drop composite FKs FIRST — they depend on the UQ constraints we remove below.
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_node_same_org")
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_pool_same_org")
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS fk_browser_sessions_project_id_projects")
    op.execute("ALTER TABLE runtime_nodes DROP CONSTRAINT IF EXISTS fk_runtime_nodes_runtime_same_org")
    op.execute("ALTER TABLE runtime_nodes DROP CONSTRAINT IF EXISTS fk_runtime_nodes_project_id_projects")

    # Restore simple (non-composite) FKs.
    op.execute("ALTER TABLE runtime_nodes ADD CONSTRAINT runtime_nodes_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE SET NULL")
    op.execute("ALTER TABLE runtime_nodes ADD CONSTRAINT fk_runtime_nodes_runtime_id_compute_runtimes FOREIGN KEY (runtime_id) REFERENCES compute_runtimes (id) ON DELETE CASCADE")

    # Drop UQ constraints (now safe, no FKs depend on them).
    op.execute("ALTER TABLE runtime_nodes DROP CONSTRAINT IF EXISTS uq_runtime_nodes_org_id")
    op.execute("ALTER TABLE compute_runtimes DROP CONSTRAINT IF EXISTS uq_compute_runtimes_org_id")
    op.execute("ALTER TABLE browser_sessions DROP CONSTRAINT IF EXISTS uq_browser_sessions_org_id")
    op.execute("ALTER TABLE browser_pools DROP CONSTRAINT IF EXISTS uq_browser_pools_org_id")

    # Restore browser_sessions simple FKs.
    op.execute("ALTER TABLE browser_sessions ADD CONSTRAINT fk_browser_sessions_pool_id_browser_pools FOREIGN KEY (pool_id) REFERENCES browser_pools (id) ON DELETE CASCADE")
    op.execute("ALTER TABLE browser_sessions ADD CONSTRAINT browser_sessions_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE SET NULL")
    op.execute("ALTER TABLE browser_sessions ADD CONSTRAINT fk_browser_sessions_node_id_runtime_nodes FOREIGN KEY (node_id) REFERENCES runtime_nodes (id)")

    # Restore browser_pools FK.
    op.execute("ALTER TABLE browser_pools DROP CONSTRAINT IF EXISTS fk_browser_pools_project_id_projects")
    op.execute("ALTER TABLE browser_pools ADD CONSTRAINT browser_pools_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE SET NULL")

    # Restore compute_runtimes FK.
    op.execute("ALTER TABLE compute_runtimes DROP CONSTRAINT IF EXISTS fk_compute_runtimes_project_id_projects")
    op.execute("ALTER TABLE compute_runtimes ADD CONSTRAINT compute_runtimes_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE SET NULL")

    # Restore mesh FKs.
    op.execute("ALTER TABLE mesh_networks DROP CONSTRAINT IF EXISTS fk_mesh_networks_project_id_projects")
    op.execute("ALTER TABLE mesh_networks ADD CONSTRAINT mesh_networks_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE SET NULL")
    op.execute("ALTER TABLE mesh_nodes DROP CONSTRAINT IF EXISTS fk_mesh_nodes_project_id_projects")
    op.execute("ALTER TABLE mesh_nodes ADD CONSTRAINT fk_mesh_nodes_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE SET NULL")

    # Revert projects UQ constraint renames.
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS uq_projects_organization_id_id")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS uq_projects_organization_id_name")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS uq_projects_organization_id_slug")
    op.execute("ALTER TABLE projects ADD CONSTRAINT uq_projects_org_id UNIQUE (organization_id, id)")
    op.execute("ALTER TABLE projects ADD CONSTRAINT uq_projects_org_name UNIQUE (organization_id, name)")
    op.execute("ALTER TABLE projects ADD CONSTRAINT uq_projects_org_slug UNIQUE (organization_id, slug)")

    # Revert projects org FK (restore CASCADE).
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS fk_projects_organization_id_organizations")
    op.execute("ALTER TABLE projects ADD CONSTRAINT fk_projects_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES organizations (id) ON DELETE CASCADE")

    # Revert FORCE RLS.
    for _table in [
        "browser_pools", "browser_sessions", "compute_runtimes",
        "document_collections", "document_pages",
        "mesh_networks", "mesh_nodes", "projects",
        "resource_shares", "runtime_nodes",
    ]:
        op.execute(f"ALTER TABLE {_table} NO FORCE ROW LEVEL SECURITY")
