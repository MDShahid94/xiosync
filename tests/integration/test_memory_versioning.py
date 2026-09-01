"""Versioned, append-only ``memory`` semantics (H6, INV-MEM-2, doc 06 §6).

Proves on the real migrated schema that ``memory`` is versioned rather than
overwritten:

* the single permitted mutation — pointing a row's ``superseded_by`` at its
  successor version, once — is allowed;
* every other mutation is rejected by the revision-0006
  ``trg_memory_versioning`` trigger: changing any content column, repointing an
  already-superseded row, clearing ``superseded_by``, and any ``DELETE``; and
* the application role never even holds ``DELETE`` on ``memory`` (privilege
  boundary), so a delete is denied for lack of privilege before the trigger.

All writes go through the canonical ``org_scoped_session`` RLS gate as the plain
application role; seeding of the org/actor uses the admin channel.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id

pytestmark = pytest.mark.integration

_LEAST_PRIV_PASSWORD = "xiosync-least-privilege"  # noqa: S105


def _seed_org_with_actor(admin_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one active org with one active actor; return (org_id, actor_id)."""
    organization_id = new_id()
    actor_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'Memory Versioning', 'active')"
                ),
                {"id": organization_id, "slug": f"memory-{organization_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO actors (id, organization_id, actor_type, state, "
                    "lifecycle_phase, trust_tier, health_status) VALUES "
                    "(:id, :org, 'ai', 'active', 'operational', 'trusted', 'healthy')"
                ),
                {"id": actor_id, "org": organization_id},
            )
    finally:
        engine.dispose()
    return organization_id, actor_id


def _context(organization_id: uuid.UUID, actor_id: uuid.UUID) -> OrgContext:
    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=actor_id,
        organization_id=organization_id,
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_MEMBER,
    )


def _insert_memory(
    engine: Engine, context: OrgContext, *, version: int, content: str
) -> uuid.UUID:
    """Insert one ``memory`` row within the org scope; return its id."""
    memory_id = new_id()
    with org_scoped_session(engine, context) as session:
        session.execute(
            text(
                "INSERT INTO memory "
                "(id, organization_id, owner_actor_id, kind, content, visibility, version) "
                "VALUES (:id, :org, :owner, 'fact', CAST(:content AS jsonb), 'private', :version)"
            ),
            {
                "id": memory_id,
                "org": context.organization_id,
                "owner": context.actor_id,
                "content": content,
                "version": version,
            },
        )
    return memory_id


@contextmanager
def _least_privilege_memory_url(admin_url: str) -> Iterator[str]:
    """A role granted ``SELECT, INSERT, UPDATE`` on memory but never ``DELETE``.

    Mirrors the doc 06 §6 grant posture: memory keeps UPDATE (to set
    ``superseded_by``) yet is never deletable by the application role.
    """
    url = make_url(admin_url)
    role_name = f"xiosync_mem_{uuid.uuid4().hex}"
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD '{_LEAST_PRIV_PASSWORD}' "
                    "NOSUPERUSER NOBYPASSRLS"
                )
            )
            connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role_name}"'))
            # PG 16+: CREATEROLE gives ADMIN but not membership; DROP OWNED BY
            # requires membership. Grant the role to the admin user.
            connection.execute(text(f'GRANT "{role_name}" TO CURRENT_USER'))
            connection.execute(text(f'GRANT SELECT, INSERT, UPDATE ON memory TO "{role_name}"'))
        try:
            yield url.set(username=role_name, password=_LEAST_PRIV_PASSWORD).render_as_string(
                hide_password=False
            )
        finally:
            with engine.connect() as connection:
                connection.execute(text(f'DROP OWNED BY "{role_name}"'))
                connection.execute(text(f'DROP ROLE "{role_name}"'))
    finally:
        engine.dispose()


def test_supersession_pointer_update_is_allowed(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-MEM-2: a new version is a new row; the prior row is pointed at it."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        v1 = _insert_memory(engine, context, version=1, content='{"k": "v1"}')
        v2 = _insert_memory(engine, context, version=2, content='{"k": "v2"}')

        # The one permitted mutation: NULL -> successor id.
        with org_scoped_session(engine, context) as session:
            session.execute(
                text("UPDATE memory SET superseded_by = :v2 WHERE id = :v1"),
                {"v2": v2, "v1": v1},
            )

        with org_scoped_session(engine, context) as session:
            rows = session.execute(
                text(
                    "SELECT id, superseded_by, content FROM memory "
                    "WHERE id IN (:v1, :v2) ORDER BY version"
                ),
                {"v1": v1, "v2": v2},
            ).all()
        by_id = {row[0]: row for row in rows}
        assert by_id[v1][1] == v2  # v1 now superseded by v2
        assert by_id[v2][1] is None  # the head version supersedes nothing
        assert by_id[v1][2] == {"k": "v1"}  # original content retained verbatim
    finally:
        engine.dispose()


def test_content_mutation_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """A content edit (no supersession) is rejected — memory is not overwritten."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        v1 = _insert_memory(engine, context, version=1, content='{"k": "v1"}')

        with pytest.raises(DBAPIError):
            with org_scoped_session(engine, context) as session:
                session.execute(
                    text("UPDATE memory SET content = CAST(:c AS jsonb) WHERE id = :id"),
                    {"c": '{"k": "tampered"}', "id": v1},
                )

        with org_scoped_session(engine, context) as session:
            content = session.execute(
                text("SELECT content FROM memory WHERE id = :id"), {"id": v1}
            ).scalar_one()
        assert content == {"k": "v1"}
    finally:
        engine.dispose()


def test_repointing_a_superseded_row_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """A row's ``superseded_by`` may be set once; a superseded row is frozen."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        v1 = _insert_memory(engine, context, version=1, content='{"k": "v1"}')
        v2 = _insert_memory(engine, context, version=2, content='{"k": "v2"}')
        v3 = _insert_memory(engine, context, version=3, content='{"k": "v3"}')

        with org_scoped_session(engine, context) as session:
            session.execute(
                text("UPDATE memory SET superseded_by = :v2 WHERE id = :v1"),
                {"v2": v2, "v1": v1},
            )

        # Re-pointing v1 (already superseded) at a different version is rejected.
        with pytest.raises(DBAPIError):
            with org_scoped_session(engine, context) as session:
                session.execute(
                    text("UPDATE memory SET superseded_by = :v3 WHERE id = :v1"),
                    {"v3": v3, "v1": v1},
                )

        with org_scoped_session(engine, context) as session:
            superseded_by = session.execute(
                text("SELECT superseded_by FROM memory WHERE id = :id"), {"id": v1}
            ).scalar_one()
        assert superseded_by == v2  # unchanged
    finally:
        engine.dispose()


def test_clearing_supersession_pointer_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """Clearing ``superseded_by`` back to NULL is rejected (append-only)."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        v1 = _insert_memory(engine, context, version=1, content='{"k": "v1"}')

        with pytest.raises(DBAPIError):
            with org_scoped_session(engine, context) as session:
                session.execute(
                    text("UPDATE memory SET superseded_by = NULL WHERE id = :id"),
                    {"id": v1},
                )
    finally:
        engine.dispose()


def test_delete_is_rejected_by_trigger(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """A DELETE fails even for the fully-privileged role (trigger backstop)."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        v1 = _insert_memory(engine, context, version=1, content='{"k": "v1"}')

        with pytest.raises(DBAPIError):
            with org_scoped_session(engine, context) as session:
                session.execute(text("DELETE FROM memory WHERE id = :id"), {"id": v1})

        with org_scoped_session(engine, context) as session:
            count = session.execute(
                text("SELECT count(*) FROM memory WHERE id = :id"), {"id": v1}
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()


def test_least_privilege_role_cannot_delete_memory(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EVENT-DB-1 analogue: the app role holds no DELETE on memory.

    The role can still write a new version (INSERT) and set ``superseded_by``
    (UPDATE), but a DELETE is denied for lack of privilege.
    """
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)

    # Seed one row with the fully-privileged fixture role.
    seed_engine = create_engine(app_role_database_url)
    try:
        v1 = _insert_memory(seed_engine, context, version=1, content='{"k": "v1"}')
    finally:
        seed_engine.dispose()

    with _least_privilege_memory_url(migrated_database_url) as least_priv_url:
        engine = create_engine(least_priv_url)
        try:
            # It CAN insert a new version (append is allowed).
            v2 = _insert_memory(engine, context, version=2, content='{"k": "v2"}')
            # It CAN set the supersession pointer (the one permitted update).
            with org_scoped_session(engine, context) as session:
                session.execute(
                    text("UPDATE memory SET superseded_by = :v2 WHERE id = :v1"),
                    {"v2": v2, "v1": v1},
                )
            # It CANNOT delete — permission denied for table memory.
            with pytest.raises(DBAPIError):
                with org_scoped_session(engine, context) as session:
                    session.execute(text("DELETE FROM memory WHERE id = :id"), {"id": v2})
        finally:
            engine.dispose()

    # Both versions survive.
    engine = create_engine(app_role_database_url)
    try:
        with org_scoped_session(engine, context) as session:
            count = session.execute(text("SELECT count(*) FROM memory")).scalar_one()
        assert count == 2
    finally:
        engine.dispose()
