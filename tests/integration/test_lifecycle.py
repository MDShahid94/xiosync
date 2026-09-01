"""Real-PostgreSQL tests for the actor lifecycle service (doc 03 §4).

Runs the full ``LifecycleService`` stack — the pure actor state machine plus the
Operation and Event writers — against a scratch database migrated by Alembic
alone, entered through the canonical ``org_scoped_session`` RLS gate. Seeding
uses the admin (superuser) connection; every service call and assertion runs as
the plain application role inside the org scope.

These prove, on the real schema:

* INV-LC-1 — a transition the actor state machine does not declare is rejected
  and leaves the actor's state and provenance untouched, and
* INV-LC-2 — every legal transition writes both an Operation and a
  ``state_change`` Event, tied together by ``operation_id``, atomically with the
  actor's state change.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.domain.lifecycle import IllegalTransitionError
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.lifecycle import ActorNotFoundError, LifecycleService

pytestmark = pytest.mark.integration


def _seed_org(admin_url: str) -> uuid.UUID:
    organization_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'Lifecycle Test', 'active')"
                ),
                {"id": organization_id, "slug": f"lc-{organization_id}"},
            )
    finally:
        engine.dispose()
    return organization_id


def _seed_actor(
    admin_url: str,
    organization_id: uuid.UUID,
    *,
    state: str,
    phase: str,
) -> uuid.UUID:
    """Seed one actor at an explicit lifecycle ``state``/``phase``."""
    actor_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO actors (id, organization_id, actor_type, state, "
                    "lifecycle_phase, trust_tier, health_status) VALUES "
                    "(:id, :org, 'ai', :state, :phase, 'trusted', 'healthy')"
                ),
                {"id": actor_id, "org": organization_id, "state": state, "phase": phase},
            )
    finally:
        engine.dispose()
    return actor_id


def _context(organization_id: uuid.UUID, actor_id: uuid.UUID) -> OrgContext:
    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=actor_id,
        organization_id=organization_id,
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_MEMBER,
    )


def _actor_state(engine: Engine, context: OrgContext, actor_id: uuid.UUID) -> tuple[str, str]:
    with org_scoped_session(engine, context) as session:
        row = session.execute(
            text("SELECT state, lifecycle_phase FROM actors WHERE id = :id"),
            {"id": actor_id},
        ).one()
    return str(row[0]), str(row[1])


def _counts(engine: Engine, context: OrgContext) -> tuple[int, int]:
    with org_scoped_session(engine, context) as session:
        operations = session.execute(text("SELECT count(*) FROM operations")).scalar_one()
        events = session.execute(text("SELECT count(*) FROM events")).scalar_one()
    return int(operations), int(events)


def test_legal_transition_writes_operation_and_event(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-LC-2: a legal transition updates the actor and writes both records."""
    organization_id = _seed_org(migrated_database_url)
    actor_id = _seed_actor(
        migrated_database_url, organization_id, state="active", phase="operational"
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        with org_scoped_session(engine, context) as session:
            service = LifecycleService(session)
            result = service.transition_actor(context, actor_id=actor_id, to_state="suspended")

        assert result.from_state == "active"
        assert result.to_state == "suspended"
        assert result.lifecycle_phase == "operational"

        # The actor's state moved.
        assert _actor_state(engine, context, actor_id) == ("suspended", "operational")

        # Exactly one Operation and one Event were written (INV-LC-2).
        assert _counts(engine, context) == (1, 1)

        with org_scoped_session(engine, context) as session:
            operation = session.execute(
                text(
                    "SELECT operation, from_state, to_state, scope, outcome "
                    "FROM operations WHERE id = :id"
                ),
                {"id": result.operation_id},
            ).one()
            event = session.execute(
                text("SELECT event_type, actor_id, payload FROM events WHERE id = :id"),
                {"id": result.event_id},
            ).one()

        assert operation[0] == "actor.state_change"
        assert (operation[1], operation[2]) == ("active", "suspended")
        assert operation[3] == "actor"
        assert operation[4] == "success"

        # The Event is a state_change tied back to its Operation (INV-LC-2).
        assert event[0] == "state_change"
        assert event[1] == actor_id
        payload = event[2]
        assert payload["from_state"] == "active"
        assert payload["to_state"] == "suspended"
        assert payload["operation_id"] == str(result.operation_id)
    finally:
        engine.dispose()


def test_illegal_transition_is_rejected_and_writes_nothing(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-LC-1: an undeclared transition changes no state and no provenance."""
    organization_id = _seed_org(migrated_database_url)
    # 'active' -> 'archived' is not a declared transition (must go via
    # terminating -> terminated -> archived).
    actor_id = _seed_actor(
        migrated_database_url, organization_id, state="active", phase="operational"
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        with pytest.raises(IllegalTransitionError):
            with org_scoped_session(engine, context) as session:
                service = LifecycleService(session)
                service.transition_actor(context, actor_id=actor_id, to_state="archived")

        # State is untouched and no Operation/Event was written (INV-LC-1).
        assert _actor_state(engine, context, actor_id) == ("active", "operational")
        assert _counts(engine, context) == (0, 0)
    finally:
        engine.dispose()


def test_full_birth_sequence_is_legal(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """Every declared step from proposed to active is legal and leaves provenance."""
    organization_id = _seed_org(migrated_database_url)
    actor_id = _seed_actor(
        migrated_database_url, organization_id, state="proposed", phase="pre_birth"
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)

    sequence = [
        ("designing", "pre_birth"),
        ("implementing", "pre_birth"),
        ("validating", "pre_birth"),
        ("initializing", "birth"),
        ("active", "operational"),
    ]
    try:
        for to_state, expected_phase in sequence:
            with org_scoped_session(engine, context) as session:
                service = LifecycleService(session)
                result = service.transition_actor(context, actor_id=actor_id, to_state=to_state)
            assert result.to_state == to_state
            assert result.lifecycle_phase == expected_phase

        assert _actor_state(engine, context, actor_id) == ("active", "operational")
        # One Operation + one Event per transition (INV-LC-2).
        assert _counts(engine, context) == (len(sequence), len(sequence))
    finally:
        engine.dispose()


def test_transition_unknown_actor_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """A transition targeting a non-existent actor is rejected cleanly."""
    organization_id = _seed_org(migrated_database_url)
    # Context actor exists; the transition target does not.
    context_actor = _seed_actor(
        migrated_database_url, organization_id, state="active", phase="operational"
    )
    context = _context(organization_id, context_actor)
    engine = create_engine(app_role_database_url)
    try:
        with pytest.raises(ActorNotFoundError):
            with org_scoped_session(engine, context) as session:
                service = LifecycleService(session)
                service.transition_actor(context, actor_id=new_id(), to_state="suspended")
    finally:
        engine.dispose()
