"""Append-only ``events`` — trigger backstop AND privilege boundary (H6).

Proves doc 03 INV-EVENT-1 / doc 06 §6 (INV-EVENT-DB-1) on the real migrated
schema, from two independent angles:

* **Trigger backstop.** Even a fully-privileged role (the ``app_role`` fixture
  is granted UPDATE/DELETE on every table) cannot mutate ``events``: the
  revision-0004 ``trg_events_append_only`` trigger raises on every UPDATE and
  DELETE, and the row survives untouched.
* **Privilege boundary.** A correctly provisioned application role holds
  ``SELECT, INSERT`` only on ``events`` — it never even carries UPDATE/DELETE.
  With that grant posture the database rejects a mutation attempt for *lack of
  privilege*, before any trigger runs. This is the doc 06 posture that revision
  0006 makes explicit by revoking UPDATE/DELETE on ``events`` from ``PUBLIC``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.events import EventService

pytestmark = pytest.mark.integration

# Throwaway credential for the least-privilege role minted below; it guards
# nothing (dropped in teardown) and authenticates only against the scratch DB.
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
                    "VALUES (:id, :slug, 'Events Immutability', 'active')"
                ),
                {"id": organization_id, "slug": f"events-imm-{organization_id}"},
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


def _append_heartbeat(engine_url: str, context: OrgContext) -> uuid.UUID:
    engine = create_engine(engine_url)
    try:
        with org_scoped_session(engine, context) as session:
            return EventService(session).append(
                context,
                event_type="heartbeat",
                payload={"summary": "tick", "severity": "info"},
                actor_id=context.actor_id,
            )
    finally:
        engine.dispose()


@contextmanager
def _least_privilege_events_url(admin_url: str) -> Iterator[str]:
    """A NOSUPERUSER/NOBYPASSRLS role granted ``SELECT, INSERT`` on events only.

    This mirrors the doc 06 INV-EVENT-DB-1 production grant posture: the app
    role is never handed UPDATE/DELETE on ``events``. Role provisioning is test
    infrastructure (it issues no table DDL), exactly like the ``app_role``
    fixture in ``conftest``.
    """
    url = make_url(admin_url)
    role_name = f"xiosync_evt_ro_{uuid.uuid4().hex}"
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
            # The whole point: SELECT + INSERT, never UPDATE/DELETE.
            connection.execute(text(f'GRANT SELECT, INSERT ON events TO "{role_name}"'))
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


def test_event_update_is_rejected_by_trigger(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EVENT-1: an UPDATE fails even for a fully-privileged role (trigger)."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    event_id = _append_heartbeat(app_role_database_url, context)

    engine = create_engine(app_role_database_url)
    try:
        with pytest.raises(DBAPIError):
            with org_scoped_session(engine, context) as session:
                session.execute(
                    text("UPDATE events SET event_type = 'metric' WHERE id = :id"),
                    {"id": event_id},
                )
        with org_scoped_session(engine, context) as session:
            event_type = session.execute(
                text("SELECT event_type FROM events WHERE id = :id"), {"id": event_id}
            ).scalar_one()
        assert event_type == "heartbeat"
    finally:
        engine.dispose()


def test_event_delete_is_rejected_by_trigger(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EVENT-1: a DELETE fails even for a fully-privileged role (trigger)."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    event_id = _append_heartbeat(app_role_database_url, context)

    engine = create_engine(app_role_database_url)
    try:
        with pytest.raises(DBAPIError):
            with org_scoped_session(engine, context) as session:
                session.execute(
                    text("DELETE FROM events WHERE id = :id"), {"id": event_id}
                )
        with org_scoped_session(engine, context) as session:
            count = session.execute(
                text("SELECT count(*) FROM events WHERE id = :id"), {"id": event_id}
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()


def test_least_privilege_role_cannot_update_or_delete_events(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EVENT-DB-1: the SELECT/INSERT-only app role is denied UPDATE/DELETE.

    The failure here is a *privilege* error raised before the trigger runs,
    proving immutability is a privilege boundary and not merely a trigger.
    """
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    event_id = _append_heartbeat(app_role_database_url, context)

    with _least_privilege_events_url(migrated_database_url) as least_priv_url:
        engine = create_engine(least_priv_url)
        try:
            # It CAN read within its org scope (SELECT is granted).
            with org_scoped_session(engine, context) as session:
                assert (
                    session.execute(
                        text("SELECT count(*) FROM events WHERE id = :id"), {"id": event_id}
                    ).scalar_one()
                    == 1
                )
            # It CANNOT update — permission denied for table events.
            with pytest.raises(DBAPIError):
                with org_scoped_session(engine, context) as session:
                    session.execute(
                        text("UPDATE events SET event_type = 'metric' WHERE id = :id"),
                        {"id": event_id},
                    )
            # It CANNOT delete — permission denied for table events.
            with pytest.raises(DBAPIError):
                with org_scoped_session(engine, context) as session:
                    session.execute(
                        text("DELETE FROM events WHERE id = :id"), {"id": event_id}
                    )
        finally:
            engine.dispose()

    # The row is intact after every rejected mutation.
    engine = create_engine(app_role_database_url)
    try:
        with org_scoped_session(engine, context) as session:
            event_type = session.execute(
                text("SELECT event_type FROM events WHERE id = :id"), {"id": event_id}
            ).scalar_one()
        assert event_type == "heartbeat"
    finally:
        engine.dispose()
