"""Wave 1 — X-1: verify FORCE ROW LEVEL SECURITY on 0008/0009 tables.

Migration 0010 adds ``FORCE ROW LEVEL SECURITY`` to the six tables that
previously only had ``ENABLE``.  Without ``FORCE``, a table owner (the role
that created the table) bypasses all RLS policies entirely.  This test verifies
that ``relforcerowsecurity`` is ``true`` for every one of these tables after
migration.

This is a schema-level invariant test: it does not need to exercise DML;
it only inspects ``pg_class`` metadata.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa


# Tables that had ENABLE-only in 0008/0009, now fixed by 0010.
_FORCE_RLS_TABLES = (
    "worker_enrollments",
    "worker_credentials",
    "plugins",
    "plugin_rpc_methods",
    "plugin_installations",
    "plugin_network_allow_rules",
)


@pytest.mark.integration
def test_force_rls_enabled_on_all_0008_0009_tables(
    migrated_database_url: str,
) -> None:
    """All six tables from migrations 0008/0009 must have
    ``relforcerowsecurity = true`` in ``pg_class`` after migration 0010.
    """
    engine = sa.create_engine(migrated_database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT relname, relforcerowsecurity "
                "FROM pg_class "
                "WHERE relname = ANY(:tables) "
                "ORDER BY relname"
            ),
            {"tables": list(_FORCE_RLS_TABLES)},
        ).fetchall()

    found = {row[0]: row[1] for row in rows}

    # Every table must be present and have FORCE RLS enabled.
    for table in _FORCE_RLS_TABLES:
        assert table in found, f"table {table!r} not found in pg_class"
        assert found[table] is True, (
            f"table {table!r} has relforcerowsecurity=False — "
            "FORCE ROW LEVEL SECURITY is missing (Gap X-1)"
        )


@pytest.mark.integration
def test_all_tenant_tables_have_force_rls(
    migrated_database_url: str,
) -> None:
    """Every table that has ENABLE RLS must also have FORCE RLS.

    This is a regression guard: future migrations that add RLS-enabled tables
    must not repeat the 0008/0009 omission.
    """
    engine = sa.create_engine(migrated_database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class "
                "WHERE relrowsecurity = true "
                "ORDER BY relname"
            )
        ).fetchall()

    violations = [
        row[0] for row in rows if row[1] is True and row[2] is False
    ]
    assert violations == [], (
        f"Tables have ENABLE RLS but not FORCE RLS: {violations}. "
        "Add FORCE ROW LEVEL SECURITY in the migration."
    )
