"""0022 — Correct XIOBR RLS policies to use the canonical ``app.current_org`` GUC.

Revision ID: 0022
Revises: 0021

Migrations 0018–0021 created RLS policies keyed on ``app.current_org_id``, but
the platform's canonical setting — the one ``org_scoped_session`` sets with
``SET LOCAL`` and all earlier migrations key on — is ``app.current_org``
(INV-TENANT-3, tenancy.py ``RLS_ORG_SETTING``).  The mismatch means the plain
application role gets an "unrecognized configuration parameter" error the moment
it touches any table introduced in those migrations.

This migration replaces the affected policies with ones that key on the correct
GUC, using the same fail-closed ``NULLIF(..., '')::uuid`` form the rest of the
policy chain uses (revision 0003).
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# Canonical fail-closed predicate (matching every other migration since 0003).
_PREDICATE = "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"

# Old broken predicate used in 0018–0021 (for downgrade only).
_OLD_PREDICATE = "organization_id = current_setting('app.current_org_id')::uuid"

# (policy_name, table_name) pairs from migrations 0018–0021.
_POLICIES: list[tuple[str, str]] = [
    # 0018 — document_pages
    ("tenant_isolation_doc_collections", "document_collections"),
    ("tenant_isolation_doc_pages", "document_pages"),
    # 0019 — browser orchestration
    ("tenant_isolation_compute_runtimes", "compute_runtimes"),
    ("tenant_isolation_runtime_nodes", "runtime_nodes"),
    ("tenant_isolation_browser_pools", "browser_pools"),
    ("tenant_isolation_browser_sessions", "browser_sessions"),
    ("tenant_isolation_mesh_networks", "mesh_networks"),
    # 0020 — projects
    ("tenant_isolation_projects", "projects"),
    # 0021 — mesh_nodes
    ("tenant_isolation_mesh_nodes", "mesh_nodes"),
]


def upgrade() -> None:
    for policy_name, table in _POLICIES:
        # Drop the old (broken) policy and recreate with the correct GUC name.
        # IF EXISTS makes this idempotent in case of partial prior correction.
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
        op.execute(
            f"CREATE POLICY {policy_name} ON {table} "
            f"USING ({_PREDICATE})"
        )


def downgrade() -> None:
    # Restore the original (broken) GUC name so downgrade stays coherent with
    # the preceding migrations.
    for policy_name, table in _POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
        op.execute(
            f"CREATE POLICY {policy_name} ON {table} "
            f"USING ({_OLD_PREDICATE})"
        )
