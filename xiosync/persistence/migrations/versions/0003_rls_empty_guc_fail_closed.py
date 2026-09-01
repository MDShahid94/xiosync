"""RLS policies fail closed on an empty ``app.current_org`` setting

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-15

Hardens the revision-0002 Row-Level Security policies (doc 05 §3.2). The 0002
predicate cast the setting directly::

    key_column = current_setting('app.current_org', true)::uuid

A *never-set* setting yields NULL and matches no rows — fail closed, as
designed. But once ``set_config('app.current_org', ..., true)`` has run on a
connection and that transaction ends, PostgreSQL reverts the custom setting to
its session-level default: the **empty string**, not NULL. On a pooled
connection the next unscoped query then fails with ``invalid input syntax for
type uuid: ""`` — an error, not the zero-row fail-closed contract the tenancy
boundary documents and ``tests/integration/test_tenancy_boundary.py`` proves.

This revision alters each policy predicate to::

    key_column = NULLIF(current_setting('app.current_org', true), '')::uuid

so both the never-set (NULL) and post-transaction ('') states collapse to
NULL, match no rows, and reject all writes. No table, column, or policy name
changes; ``ALTER POLICY`` swaps only the USING/WITH CHECK expressions
(online-safe, INV-MIG-4).

Reversibility (INV-MIG-3): fully reversible — downgrade restores the exact
0002 predicate on every policy.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

# Same key-column map as revision 0002 (doc 05 §3.2): tenant-bearing tables
# are keyed on organization_id; organizations is keyed on its own id.
_RLS_KEY_COLUMN: dict[str, str] = {
    "organizations": "id",
    "actors": "organization_id",
    "auth_identities": "organization_id",
    "memberships": "organization_id",
    "sessions": "organization_id",
}


def _alter_policies(predicate_template: str) -> None:
    for table, key_column in _RLS_KEY_COLUMN.items():
        predicate = predicate_template.format(key_column=key_column)
        op.execute(
            f"ALTER POLICY rls_{table}_org_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def upgrade() -> None:
    _alter_policies("{key_column} = NULLIF(current_setting('app.current_org', true), '')::uuid")


def downgrade() -> None:
    _alter_policies("{key_column} = current_setting('app.current_org', true)::uuid")
