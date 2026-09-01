"""Real-PostgreSQL tests for the Event service (doc 03 §2.8).

Runs the full ``EventService`` stack against a scratch database migrated by
Alembic alone, entered through the canonical ``org_scoped_session`` RLS gate.
Seeding uses the admin (superuser) connection; every service call and assertion
runs as the plain application role inside the org scope.

These prove, on the real schema:

* an appended Event carries the correct structure (INV-EVENT-1) — the context's
  organization, the actor, the declared ``event_type``, and the canonical
  payload,
* an unknown ``event_type`` is rejected at the service boundary and nothing is
  written, and
* Events are append-only: an ``UPDATE`` and a ``DELETE`` on ``events`` from the
  application role both fail at the database (INV-EVENT-1).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.domain.events import InvalidEventTypeError
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.events import EventService

pytestmark = pytest.mark.integration


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
                    "VALUES (:id, :slug, 'Events Test', 'active')"
                ),
                {"id": organization_id, "slug": f"events-{organization_id}"},
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


def _append_heartbeat(engine: Engine, context: OrgContext) -> uuid.UUID:
    with org_scoped_session(engine, context) as session:
        service = EventService(session)
        return service.append(
            context,
            event_type="heartbeat",
            payload={"summary": "tick", "severity": "info"},
            actor_id=context.actor_id,
        )


def test_append_event_persists_correct_structure(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EVENT-1: an appended Event has the expected org/actor/type/payload."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        event_id = _append_heartbeat(engine, context)

        with org_scoped_session(engine, context) as session:
            service = EventService(session)
            record = service.get_event(context, event_id)

        assert record is not None
        assert record.organization_id == organization_id
        assert record.actor_id == actor_id
        assert record.event_type == "heartbeat"
        assert record.payload == {"summary": "tick", "severity": "info"}
    finally:
        engine.dispose()


def test_append_unknown_event_type_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """An unregistered event type is rejected before any row is written."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        with pytest.raises(InvalidEventTypeError):
            with org_scoped_session(engine, context) as session:
                service = EventService(session)
                service.append(
                    context,
                    event_type="not_a_real_type",
                    payload={"summary": "nope"},
                    actor_id=actor_id,
                )

        # Nothing was written: the org's event stream is empty.
        with org_scoped_session(engine, context) as session:
            count = session.execute(text("SELECT count(*) FROM events")).scalar_one()
        assert count == 0
    finally:
        engine.dispose()


def test_event_update_is_rejected_by_database(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EVENT-1: an UPDATE on events fails at the DB (append-only trigger)."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        event_id = _append_heartbeat(engine, context)

        with pytest.raises(DBAPIError):
            with org_scoped_session(engine, context) as session:
                session.execute(
                    text("UPDATE events SET event_type = 'metric' WHERE id = :id"),
                    {"id": event_id},
                )

        # The row is untouched: its type is still what it was appended with.
        with org_scoped_session(engine, context) as session:
            event_type = session.execute(
                text("SELECT event_type FROM events WHERE id = :id"),
                {"id": event_id},
            ).scalar_one()
        assert event_type == "heartbeat"
    finally:
        engine.dispose()


def test_event_delete_is_rejected_by_database(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EVENT-1: a DELETE on events fails at the DB (append-only trigger)."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        event_id = _append_heartbeat(engine, context)

        with pytest.raises(DBAPIError):
            with org_scoped_session(engine, context) as session:
                session.execute(
                    text("DELETE FROM events WHERE id = :id"),
                    {"id": event_id},
                )

        # The row survives the rejected delete.
        with org_scoped_session(engine, context) as session:
            count = session.execute(
                text("SELECT count(*) FROM events WHERE id = :id"),
                {"id": event_id},
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()
