"""0023 — Add RLS policy on ``resource_shares`` table (Gap 1.1).

Revision ID: 0023
Revises: 0022

``resource_shares`` was deliberately left without RLS in migration 0015 so
that the cross-org sharing predicates on other tables (capabilities, artifacts,
etc.) could reference it via a subquery from the *superuser* security context.

However, a direct SELECT against ``resource_shares`` from the application role
leaks every active share to any tenant.  Gap 1.1 closes this by adding a
SELECT-only policy that allows a row to be read if the current org is EITHER
the source (owner of the shared resource) OR the target (recipient of the share).

Write operations (INSERT / UPDATE / DELETE) remain service-layer controlled —
the platform connects as superuser for writes, so no write policy is added.

The dual-column predicate is:
    source_org_id = <current_org>  OR  target_org_id = <current_org>

``target_org_id IS NULL`` represents a public share — also made readable to
all tenants, as intended by the original design in 0016.

Canonical GUC (fail-closed):
    NULLIF(current_setting('app.current_org', true), '')::uuid
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

# Canonical fail-closed GUC expression — same as every migration since 0003.
_CURRENT_ORG = "NULLIF(current_setting('app.current_org', true), '')::uuid"

# SELECT policy: readable when current org is source, target, or the share is public.
_POLICY_NAME = "resource_shares_tenant_read"
_POLICY_PREDICATE = f"""(
    source_org_id = {_CURRENT_ORG}
    OR target_org_id = {_CURRENT_ORG}
    OR target_org_id IS NULL
)"""


def upgrade() -> None:
    # Enable RLS (FORCE ensures the superuser-bypasses are explicit).
    # We do NOT force RLS for writes — superuser sessions are used for writes.
    op.execute("ALTER TABLE resource_shares ENABLE ROW LEVEL SECURITY")

    # SELECT-only policy: tenants can read shares they own (source) or receive (target).
    # Public shares (target_org_id IS NULL) are visible to all authenticated tenants.
    op.execute(
        f"CREATE POLICY {_POLICY_NAME} ON resource_shares "
        f"FOR SELECT "
        f"USING {_POLICY_PREDICATE}"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON resource_shares")
    op.execute("ALTER TABLE resource_shares NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE resource_shares DISABLE ROW LEVEL SECURITY")
