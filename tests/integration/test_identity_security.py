"""Identity-slice database enforcement tests (docs 05 §3.2, 06 §4, 11).

Runs against a scratch database migrated by Alembic alone and proves, on the
real schema, the three enforcement layers revision 0002 added:

- **Drift (INV-TEST-SCHEMA-2):** autogenerate comparing the ORM metadata
  against the migrated database finds NOTHING — the migration chain and the
  models describe the same schema.
- **RLS isolation (INV-TENANT-2/-4, doc 05 §3.2):** a plain application role
  sees only ``app.current_org``'s rows, cannot write another org's rows, and
  sees nothing when the setting is absent (fail closed).
- **Immutability (doc 06 §4, C2):** IMM columns reject UPDATEs in the
  database itself — even for a superuser (triggers, unlike RLS, are not
  bypassed).

Seeding uses the admin (superuser) connection, which bypasses RLS by
PostgreSQL design; assertions about isolation always use the plain role.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from xiosync.persistence.models import Base
from xiosync.platform.ids import new_id

pytestmark = pytest.mark.integration

_ORG_1 = new_id()
_ORG_2 = new_id()
_ACTOR_1 = new_id()
_ACTOR_2 = new_id()


def _seed_two_orgs(admin_url: str) -> None:
    """Seed two organizations, one actor each, via the admin connection."""
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            for org_id, slug in ((_ORG_1, "org-one"), (_ORG_2, "org-two")):
                connection.execute(
                    text(
                        "INSERT INTO organizations (id, slug, name, state) "
                        "VALUES (:id, :slug, :slug, 'active')"
                    ),
                    {"id": org_id, "slug": slug},
                )
            for actor_id, org_id in ((_ACTOR_1, _ORG_1), (_ACTOR_2, _ORG_2)):
                connection.execute(
                    text(
                        "INSERT INTO actors (id, organization_id, actor_type, state, "
                        "lifecycle_phase, trust_tier, health_status) "
                        "VALUES (:id, :org, 'human', 'active', 'operational', "
                        "'newcomer', 'healthy')"
                    ),
                    {"id": actor_id, "org": org_id},
                )
    finally:
        engine.dispose()


@pytest.fixture()
def seeded_app_connection(
    migrated_database_url: str, app_role_database_url: str
) -> Iterator[Connection]:
    """A plain-role connection to a migrated scratch DB seeded with two orgs."""
    _seed_two_orgs(migrated_database_url)
    engine = create_engine(app_role_database_url)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def _set_current_org(connection: Connection, org_id: uuid.UUID) -> None:
    connection.execute(
        text("SELECT set_config('app.current_org', :org, false)"),
        {"org": str(org_id)},
    )


def test_autogenerate_diff_is_empty(migrated_database_url: str) -> None:
    """INV-TEST-SCHEMA-2 (drift half): metadata == migrated database."""
    engine = create_engine(migrated_database_url)
    try:
        with engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    assert diff == [], f"ORM metadata has drifted from the migration chain: {diff}"


def test_rls_scopes_reads_to_current_org(seeded_app_connection: Connection) -> None:
    """INV-TENANT-2: the plain role sees only app.current_org's rows."""
    _set_current_org(seeded_app_connection, _ORG_1)

    org_rows = seeded_app_connection.execute(text("SELECT id FROM organizations")).fetchall()
    assert [row[0] for row in org_rows] == [_ORG_1]

    actor_rows = seeded_app_connection.execute(text("SELECT id FROM actors")).fetchall()
    assert [row[0] for row in actor_rows] == [_ACTOR_1]


def test_rls_fails_closed_without_current_org(seeded_app_connection: Connection) -> None:
    """An unset app.current_org matches no rows — never 'all rows'."""
    org_count = seeded_app_connection.execute(
        text("SELECT count(*) FROM organizations")
    ).scalar_one()
    actor_count = seeded_app_connection.execute(text("SELECT count(*) FROM actors")).scalar_one()
    assert (org_count, actor_count) == (0, 0)


def test_rls_rejects_cross_org_write(seeded_app_connection: Connection) -> None:
    """INV-TENANT-4: writing another org's rows is rejected by WITH CHECK."""
    _set_current_org(seeded_app_connection, _ORG_1)

    with pytest.raises(ProgrammingError, match="row-level security policy"):
        seeded_app_connection.execute(
            text(
                "INSERT INTO actors (id, organization_id, actor_type, state, "
                "lifecycle_phase, trust_tier, health_status) "
                "VALUES (:id, :org, 'human', 'active', 'operational', "
                "'newcomer', 'healthy')"
            ),
            {"id": new_id(), "org": _ORG_2},
        )
    seeded_app_connection.rollback()


def test_imm_columns_reject_update(migrated_database_url: str) -> None:
    """Doc 06 §4 / C2: IMM columns are immutable even for a superuser."""
    _seed_two_orgs(migrated_database_url)
    engine = create_engine(migrated_database_url)
    try:
        with engine.connect() as connection:
            with pytest.raises(DBAPIError, match="immutable"):
                connection.execute(
                    text("UPDATE organizations SET slug = 'renamed' WHERE id = :id"),
                    {"id": _ORG_1},
                )
            connection.rollback()

            with pytest.raises(DBAPIError, match="immutable"):
                connection.execute(
                    text("UPDATE actors SET organization_id = :org WHERE id = :id"),
                    {"org": _ORG_2, "id": _ACTOR_1},
                )
            connection.rollback()

            # Mutable columns still update normally.
            result = connection.execute(
                text("UPDATE organizations SET name = 'Renamed Org' WHERE id = :id"),
                {"id": _ORG_1},
            )
            assert result.rowcount == 1
            connection.commit()
    finally:
        engine.dispose()
