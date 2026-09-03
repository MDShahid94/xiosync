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
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(op.f('fk_runtime_nodes_project_id_projects'), 'runtime_nodes', type_='foreignkey')
    op.drop_constraint('fk_runtime_nodes_runtime_same_org', 'runtime_nodes', type_='foreignkey')
    op.create_foreign_key(op.f('runtime_nodes_project_id_fkey'), 'runtime_nodes', 'projects', ['project_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_runtime_nodes_runtime_id_compute_runtimes'), 'runtime_nodes', 'compute_runtimes', ['runtime_id'], ['id'], ondelete='CASCADE')
    op.drop_constraint('uq_runtime_nodes_org_id', 'runtime_nodes', type_='unique')
    op.drop_constraint(op.f('fk_projects_organization_id_organizations'), 'projects', type_='foreignkey')
    op.create_foreign_key(op.f('fk_projects_organization_id_organizations'), 'projects', 'organizations', ['organization_id'], ['id'], ondelete='CASCADE')
    op.drop_constraint(op.f('uq_projects_organization_id_slug'), 'projects', type_='unique')
    op.drop_constraint(op.f('uq_projects_organization_id_name'), 'projects', type_='unique')
    op.drop_constraint(op.f('uq_projects_organization_id_id'), 'projects', type_='unique')
    op.create_unique_constraint(op.f('uq_projects_org_slug'), 'projects', ['organization_id', 'slug'], postgresql_nulls_not_distinct=False)
    op.create_unique_constraint(op.f('uq_projects_org_name'), 'projects', ['organization_id', 'name'], postgresql_nulls_not_distinct=False)
    op.create_unique_constraint(op.f('uq_projects_org_id'), 'projects', ['organization_id', 'id'], postgresql_nulls_not_distinct=False)
    op.drop_constraint(op.f('fk_mesh_nodes_project_id_projects'), 'mesh_nodes', type_='foreignkey')
    op.create_foreign_key(op.f('fk_mesh_nodes_project_id_projects'), 'mesh_nodes', 'projects', ['project_id'], ['id'], ondelete='SET NULL')
    op.drop_constraint(op.f('fk_mesh_networks_project_id_projects'), 'mesh_networks', type_='foreignkey')
    op.create_foreign_key(op.f('mesh_networks_project_id_fkey'), 'mesh_networks', 'projects', ['project_id'], ['id'], ondelete='SET NULL')
    op.drop_constraint(op.f('fk_compute_runtimes_project_id_projects'), 'compute_runtimes', type_='foreignkey')
    op.create_foreign_key(op.f('compute_runtimes_project_id_fkey'), 'compute_runtimes', 'projects', ['project_id'], ['id'], ondelete='SET NULL')
    op.drop_constraint('uq_compute_runtimes_org_id', 'compute_runtimes', type_='unique')
    op.drop_constraint('fk_browser_sessions_pool_same_org', 'browser_sessions', type_='foreignkey')
    op.drop_constraint(op.f('fk_browser_sessions_project_id_projects'), 'browser_sessions', type_='foreignkey')
    op.drop_constraint('fk_browser_sessions_node_same_org', 'browser_sessions', type_='foreignkey')
    op.create_foreign_key(op.f('fk_browser_sessions_pool_id_browser_pools'), 'browser_sessions', 'browser_pools', ['pool_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key(op.f('browser_sessions_project_id_fkey'), 'browser_sessions', 'projects', ['project_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_browser_sessions_node_id_runtime_nodes'), 'browser_sessions', 'runtime_nodes', ['node_id'], ['id'])
    op.drop_constraint('uq_browser_sessions_org_id', 'browser_sessions', type_='unique')
    op.drop_constraint(op.f('fk_browser_pools_project_id_projects'), 'browser_pools', type_='foreignkey')
    op.create_foreign_key(op.f('browser_pools_project_id_fkey'), 'browser_pools', 'projects', ['project_id'], ['id'], ondelete='SET NULL')
    op.drop_constraint('uq_browser_pools_org_id', 'browser_pools', type_='unique')
    # ### end Alembic commands ###
