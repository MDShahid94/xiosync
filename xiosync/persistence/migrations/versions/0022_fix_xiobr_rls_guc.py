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

# Tables whose RLS policies need replacing.
# The policy names follow the ``tenant_isolation_<table>`` convention from 0019–0021.
_TABLES = [
    "document_pages",       # 0018
    "document_page_chunks", # 0018
    "compute_runtimes",     # 0019
    "runtime_nodes",        # 0019
    "browser_pools",        # 0019
    "browser_sessions",     # 0019
    "mesh_networks",        # 0019
    "projects",             # 0020
    "mesh_nodes",           # 0021
]

# Canonical fail-closed predicate (matching every other migration since 0003).
_PREDICATE = "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"


def upgrade() -> None:
    for table in _TABLES:
        policy_name = f"tenant_isolation_{table}"
        # Drop the old (broken) policy and recreate with the correct GUC name.
        # Use IF EXISTS so the migration is idempotent if the policy was already
        # partially corrected.
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
        op.execute(
            f"CREATE POLICY {policy_name} ON {table} "
            f"USING ({_PREDICATE})"
        )


def downgrade() -> None:
    # Restore the (broken) original GUC name so downgrade stays coherent with
    # the preceding migrations.
    _OLD_PREDICATE = "organization_id = current_setting('app.current_org_id')::uuid"
    for table in _TABLES:
        policy_name = f"tenant_isolation_{table}"
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
        op.execute(
            f"CREATE POLICY {policy_name} ON {table} "
            f"USING ({_OLD_PREDICATE})"
        )
