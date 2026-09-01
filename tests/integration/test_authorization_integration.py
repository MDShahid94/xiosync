"""Real-PostgreSQL tests for the authorization decision point (doc 05 §4).

Runs the full ``AuthorizationService`` stack — domain policy + tenant-scoped
repository — against a scratch database migrated by Alembic alone, entered
through the canonical ``org_scoped_session`` RLS gate. Seeding uses the admin
(superuser) connection (which bypasses RLS by PostgreSQL design); every
assertion about the decision and the emitted audit row runs as the plain
application role inside the org scope.

These prove, on the real schema, what the unit suite proves against a mock:

* the normative evaluation order short-circuits (an inactive actor denies
  before the otherwise-valid grant is ever consulted), and
* a ``policy_decision`` row lands in the append-only ``events`` table on both
  allow and deny.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.authorization import AuthorizationRepository
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.authorization import AuthorizationService

pytestmark = [pytest.mark.integration, pytest.mark.security]

_NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
_CAPABILITY = "documents.read"
_SATISFYING_CONSTRAINTS = {"operations": ["read"], "resource_types": ["document"]}


def _seed(
    admin_url: str,
    *,
    actor_state: str = "active",
    org_state: str = "active",
    include_grant: bool = True,
    grant_state: str = "active",
    constraints: dict[str, object] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one org + actor + capability (+ optional grant); return (org, actor)."""
    organization_id = new_id()
    actor_id = new_id()
    capability_id = new_id()
    grant_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'Authz Test', :state)"
                ),
                {"id": organization_id, "slug": f"authz-{organization_id}", "state": org_state},
            )
            connection.execute(
                text(
                    "INSERT INTO actors (id, organization_id, actor_type, state, "
                    "lifecycle_phase, trust_tier, health_status) VALUES "
                    "(:id, :org, 'human', :state, 'operational', 'trusted', 'healthy')"
                ),
                {"id": actor_id, "org": organization_id, "state": actor_state},
            )
            connection.execute(
                text(
                    "INSERT INTO capabilities (id, organization_id, name) VALUES (:id, :org, :name)"
                ),
                {"id": capability_id, "org": organization_id, "name": _CAPABILITY},
            )
            if include_grant:
                connection.execute(
                    text(
                        "INSERT INTO grants (id, organization_id, actor_id, capability_id, "
                        "state, constraints) VALUES "
                        "(:id, :org, :actor, :cap, :state, CAST(:constraints AS JSONB))"
                    ),
                    {
                        "id": grant_id,
                        "org": organization_id,
                        "actor": actor_id,
                        "cap": capability_id,
                        "state": grant_state,
                        "constraints": json.dumps(
                            _SATISFYING_CONSTRAINTS if constraints is None else constraints
                        ),
                    },
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


def _authorize(
    engine: object,
    context: OrgContext,
    *,
    resource_organization_id: uuid.UUID,
    operation: str = "read",
    resource_type: str = "document",
) -> object:
    with org_scoped_session(engine, context) as session:  # type: ignore[arg-type]
        service = AuthorizationService(AuthorizationRepository(session))
        return service.authorize(
            context,
            capability=_CAPABILITY,
            operation=operation,
            resource_type=resource_type,
            resource_id=new_id(),
            resource_organization_id=resource_organization_id,
            arguments={},
            now=_NOW,
        )


def _policy_events(engine: object, context: OrgContext) -> list[dict[str, object]]:
    with org_scoped_session(engine, context) as session:  # type: ignore[arg-type]
        rows = session.execute(
            text(
                "SELECT payload FROM events WHERE event_type = 'policy_decision' "
                "ORDER BY created_at"
            )
        ).fetchall()
    return [row[0] for row in rows]


def test_authorize_allows_and_emits_policy_decision_event(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    organization_id, actor_id = _seed(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        decision = _authorize(engine, context, resource_organization_id=organization_id)

        assert decision.allowed is True  # type: ignore[attr-defined]
        assert decision.reason == "allowed"  # type: ignore[attr-defined]
        assert decision.grant_id is not None  # type: ignore[attr-defined]

        events = _policy_events(engine, context)
        assert len(events) == 1
        assert events[0]["allowed"] is True
        assert events[0]["reason"] == "allowed"
        assert events[0]["capability"] == _CAPABILITY
    finally:
        engine.dispose()


def test_authorize_denies_missing_grant_but_still_emits_event(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    organization_id, actor_id = _seed(migrated_database_url, include_grant=False)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        decision = _authorize(engine, context, resource_organization_id=organization_id)

        assert decision.allowed is False  # type: ignore[attr-defined]
        assert decision.reason == "grant_missing"  # type: ignore[attr-defined]

        events = _policy_events(engine, context)
        assert len(events) == 1
        assert events[0]["allowed"] is False
        assert events[0]["reason"] == "grant_missing"
    finally:
        engine.dispose()


def test_authorize_denies_cross_org_resource_before_consulting_grant(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """Step 2 (resource ownership) short-circuits before step 3 (grant)."""
    organization_id, actor_id = _seed(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        decision = _authorize(engine, context, resource_organization_id=new_id())

        assert decision.allowed is False  # type: ignore[attr-defined]
        assert decision.reason == "resource_ownership_mismatch"  # type: ignore[attr-defined]

        events = _policy_events(engine, context)
        assert len(events) == 1
        assert events[0]["allowed"] is False
        assert events[0]["reason"] == "resource_ownership_mismatch"
    finally:
        engine.dispose()


def test_inactive_actor_short_circuits_before_valid_grant(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """Step 1 (actor) runs first: a suspended actor denies despite a valid grant."""
    organization_id, actor_id = _seed(migrated_database_url, actor_state="suspended")
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        decision = _authorize(engine, context, resource_organization_id=organization_id)

        assert decision.allowed is False  # type: ignore[attr-defined]
        assert decision.reason == "actor_invalid"  # type: ignore[attr-defined]

        events = _policy_events(engine, context)
        assert len(events) == 1
        assert events[0]["reason"] == "actor_invalid"
    finally:
        engine.dispose()
