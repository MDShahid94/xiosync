"""Real-PostgreSQL tests for the Operation service (doc 03 §§2.6, 3).

Runs the full ``OperationService`` stack — pure hierarchy predicate + tenant-
scoped repository — against a scratch database migrated by Alembic alone,
entered through the canonical ``org_scoped_session`` RLS gate. Seeding uses the
admin (superuser) connection (which bypasses RLS by design); every service call
and assertion runs as the plain application role inside the org scope.

These prove, on the real schema:

* INV-OP-1 — reparenting that would close a cycle in the hierarchy graph is
  rejected on write, and
* INV-OP-2 — a parent operation from another organization is unresolvable and
  rejected (same-org referential integrity at the service boundary).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.domain.operations import HierarchyCycleError
from xiosync.persistence.operations import OperationRepository
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.operations import (
    OperationService,
    ParentOperationNotFoundError,
)

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
                    "VALUES (:id, :slug, 'Ops Test', 'active')"
                ),
                {"id": organization_id, "slug": f"ops-{organization_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO actors (id, organization_id, actor_type, state, "
                    "lifecycle_phase, trust_tier, health_status) VALUES "
                    "(:id, :org, 'human', 'active', 'operational', 'trusted', 'healthy')"
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


def _record(engine: Engine, context: OrgContext, *, parent: uuid.UUID | None = None) -> uuid.UUID:
    with org_scoped_session(engine, context) as session:
        service = OperationService(OperationRepository(session))
        return service.record_operation(
            context,
            actor_id=context.actor_id,
            operation="actor.state_change",
            trigger="system",
            initiated_by=context.actor_id,
            parent_operation_id=parent,
        )


def _depth(engine: Engine, context: OrgContext, operation_id: uuid.UUID) -> int:
    with org_scoped_session(engine, context) as session:
        depth = session.execute(
            text("SELECT depth_level FROM operations WHERE id = :id"),
            {"id": operation_id},
        ).scalar_one()
    return int(depth)


def test_record_operation_with_parent_sets_depth(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        root = _record(engine, context)
        child = _record(engine, context, parent=root)
        grandchild = _record(engine, context, parent=child)

        assert _depth(engine, context, root) == 0
        assert _depth(engine, context, child) == 1
        assert _depth(engine, context, grandchild) == 2
    finally:
        engine.dispose()


def test_set_parent_creating_cycle_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-OP-1: making a node the child of its own descendant is rejected."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        root = _record(engine, context)
        child = _record(engine, context, parent=root)

        with pytest.raises(HierarchyCycleError):
            with org_scoped_session(engine, context) as session:
                service = OperationService(OperationRepository(session))
                service.set_parent(context, root, child)

        # The rejected write left the hierarchy untouched: root is still a root.
        with org_scoped_session(engine, context) as session:
            parent = session.execute(
                text("SELECT parent_operation_id FROM operations WHERE id = :id"),
                {"id": root},
            ).scalar_one()
        assert parent is None
    finally:
        engine.dispose()


def test_set_parent_self_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-OP-1: a node cannot be its own parent (degenerate cycle)."""
    organization_id, actor_id = _seed_org_with_actor(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        node = _record(engine, context)
        with pytest.raises(HierarchyCycleError):
            with org_scoped_session(engine, context) as session:
                service = OperationService(OperationRepository(session))
                service.set_parent(context, node, node)
    finally:
        engine.dispose()


def test_parent_from_another_org_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-OP-2: a cross-org parent is unresolvable inside the tenant scope."""
    org_a, actor_a = _seed_org_with_actor(migrated_database_url)
    org_b, actor_b = _seed_org_with_actor(migrated_database_url)
    context_a = _context(org_a, actor_a)
    context_b = _context(org_b, actor_b)
    engine = create_engine(app_role_database_url)
    try:
        foreign_parent = _record(engine, context_b)

        with pytest.raises(ParentOperationNotFoundError):
            with org_scoped_session(engine, context_a) as session:
                service = OperationService(OperationRepository(session))
                service.record_operation(
                    context_a,
                    actor_id=actor_a,
                    operation="actor.state_change",
                    trigger="system",
                    initiated_by=actor_a,
                    parent_operation_id=foreign_parent,
                )
    finally:
        engine.dispose()
