"""M-2 Phase 2 — cross-org sharing RLS policy updates.

Revision ID: 0016
Revises: 0015

Updates RLS policies on shareable tables (capabilities, artifacts, workflows,
plugins) to allow read access through active ``resource_shares`` entries.

The pattern:
  - DROP the existing single-org-only ``org_isolation`` policy
  - CREATE a new ``org_isolation_with_sharing`` policy that allows access when:
    a) the row belongs to the current org (normal path), OR
    b) an active, non-expired ``resource_shares`` entry grants access to the
       current org (or is a public share with target_org_id IS NULL)

Write operations (INSERT/UPDATE/DELETE) remain strictly org-scoped — only
SELECT is relaxed for shared resources.
"""

from __future__ import annotations

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None

_ORG_ISOLATION = (
    "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)

# Tables that support cross-org sharing via resource_shares.
_SHAREABLE_TABLES = ("capabilities", "artifacts", "workflows", "plugins")


def _sharing_predicate(table: str, resource_type: str) -> str:
    """Build RLS USING predicate that includes resource_shares lookup.

    SELECT: allow if own-org OR shared via active resource_shares.
    INSERT/UPDATE/DELETE: own-org only.
    """
    return f"""(
        {_ORG_ISOLATION}
        OR EXISTS (
            SELECT 1 FROM resource_shares rs
            WHERE rs.resource_type = '{resource_type}'
              AND rs.resource_id = {table}.id
              AND rs.state = 'active'
              AND (rs.expires_at IS NULL OR rs.expires_at > now())
              AND (
                  rs.target_org_id = NULLIF(current_setting('app.current_org', true), '')::uuid
                  OR rs.target_org_id IS NULL
              )
        )
    )"""


def upgrade() -> None:
    for table in _SHAREABLE_TABLES:
        # The resource_type in resource_shares matches the table name
        # (singular form) per the check constraint.
        resource_type = table.rstrip("s")  # capabilities -> capability, etc.

        # 1. Drop old SELECT-inclusive policy
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        # Also drop the rls_ prefixed variant from early migrations
        op.execute(f"DROP POLICY IF EXISTS rls_{table}_org_isolation ON {table}")

        # 2. Create write-only policy (INSERT/UPDATE/DELETE = own org only)
        op.execute(
            f"CREATE POLICY org_write_isolation ON {table} "
            f"FOR ALL "
            f"USING ({_ORG_ISOLATION}) "
            f"WITH CHECK ({_ORG_ISOLATION})"
        )

        # 3. Create read policy that includes sharing
        op.execute(
            f"CREATE POLICY org_read_with_sharing ON {table} "
            f"FOR SELECT "
            f"USING {_sharing_predicate(table, resource_type)}"
        )


def downgrade() -> None:
    for table in _SHAREABLE_TABLES:
        # Remove sharing-aware policies
        op.execute(f"DROP POLICY IF EXISTS org_read_with_sharing ON {table}")
        op.execute(f"DROP POLICY IF EXISTS org_write_isolation ON {table}")

        # Restore original simple org isolation
        op.execute(
            f"CREATE POLICY org_isolation ON {table} "
            f"USING ({_ORG_ISOLATION})"
        )
