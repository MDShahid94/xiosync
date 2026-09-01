"""Real-PostgreSQL tests for the Edge & Memory services (doc 03 §§2.7, 2.9, 3).

Runs the full ``EdgeService`` / ``MemoryService`` stack — pure ontology
predicates + tenant-scoped repositories — against a scratch database migrated
by Alembic alone, entered through the canonical ``org_scoped_session`` RLS gate.
Seeding uses the admin (superuser) connection; every service call and assertion
runs as the plain application role inside the org scope.

These prove, on the real schema:

* INV-EDGE-1 / INV-MEM-1 — an edge endpoint or memory owner in another org is
  rejected at the service boundary (same-org referential integrity),
* INV-EDGE-2 — a cycle in an acyclic graph class is rejected while a cycle in
  the ``relationship`` class is allowed, and
* INV-MEM-2 — a memory update writes a new version row and points the prior
  row's ``superseded_by`` at it, leaving the original otherwise intact.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.domain.ontology import GraphCycleError
from xiosync.persistence.ontology import EdgeRepository, MemoryRepository
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.ontology import (
    ActorNotInOrganizationError,
    EdgeService,
    MemoryAlreadySupersededError,
    MemoryService,
)

pytestmark = pytest.mark.integration


def _seed_org(admin_url: str) -> uuid.UUID:
    organization_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'Ontology Test', 'active')"
                ),
                {"id": organization_id, "slug": f"onto-{organization_id}"},
            )
    finally:
        engine.dispose()
    return organization_id


def _seed_actor(admin_url: str, organization_id: uuid.UUID) -> uuid.UUID:
    actor_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
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


def _create_edge(
    engine: Engine,
    context: OrgContext,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    graph_class: str,
    edge_type: str = "manages",
) -> uuid.UUID:
    with org_scoped_session(engine, context) as session:
        service = EdgeService(EdgeRepository(session))
        return service.create_edge(
            context,
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            graph_class=graph_class,
        )


# -- Edges -----------------------------------------------------------------


def test_create_edge_same_org_succeeds(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    organization_id = _seed_org(migrated_database_url)
    source_id = _seed_actor(migrated_database_url, organization_id)
    target_id = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, source_id)
    engine = create_engine(app_role_database_url)
    try:
        edge_id = _create_edge(
            engine, context, source_id=source_id, target_id=target_id, graph_class="hierarchy"
        )
        with org_scoped_session(engine, context) as session:
            count = session.execute(
                text("SELECT count(*) FROM edges WHERE id = :id"), {"id": edge_id}
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()


def test_create_edge_cross_org_target_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EDGE-1: an endpoint in another org is rejected before insert."""
    org_a = _seed_org(migrated_database_url)
    org_b = _seed_org(migrated_database_url)
    source_id = _seed_actor(migrated_database_url, org_a)
    foreign_target = _seed_actor(migrated_database_url, org_b)
    context = _context(org_a, source_id)
    engine = create_engine(app_role_database_url)
    try:
        with pytest.raises(ActorNotInOrganizationError):
            _create_edge(
                engine,
                context,
                source_id=source_id,
                target_id=foreign_target,
                graph_class="hierarchy",
            )
    finally:
        engine.dispose()


def test_hierarchy_cycle_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EDGE-2: closing a cycle in an acyclic graph class is rejected."""
    organization_id = _seed_org(migrated_database_url)
    x = _seed_actor(migrated_database_url, organization_id)
    y = _seed_actor(migrated_database_url, organization_id)
    z = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, x)
    engine = create_engine(app_role_database_url)
    try:
        _create_edge(engine, context, source_id=x, target_id=y, graph_class="hierarchy")
        _create_edge(engine, context, source_id=y, target_id=z, graph_class="hierarchy")

        with pytest.raises(GraphCycleError):
            _create_edge(engine, context, source_id=z, target_id=x, graph_class="hierarchy")
    finally:
        engine.dispose()


def test_relationship_cycle_is_allowed(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """The ``relationship`` graph class permits cycles (doc 03 §3)."""
    organization_id = _seed_org(migrated_database_url)
    x = _seed_actor(migrated_database_url, organization_id)
    y = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, x)
    engine = create_engine(app_role_database_url)
    try:
        _create_edge(
            engine,
            context,
            source_id=x,
            target_id=y,
            graph_class="relationship",
            edge_type="collaborates_with",
        )
        # The reverse edge closes a cycle, which is legal for this class.
        edge_id = _create_edge(
            engine,
            context,
            source_id=y,
            target_id=x,
            graph_class="relationship",
            edge_type="collaborates_with",
        )
        with org_scoped_session(engine, context) as session:
            count = session.execute(
                text("SELECT count(*) FROM edges WHERE id = :id"), {"id": edge_id}
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()


# -- Memory ----------------------------------------------------------------


def test_create_memory_cross_org_owner_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-MEM-1: a memory owner in another org is rejected before insert."""
    org_a = _seed_org(migrated_database_url)
    org_b = _seed_org(migrated_database_url)
    actor_a = _seed_actor(migrated_database_url, org_a)
    foreign_owner = _seed_actor(migrated_database_url, org_b)
    context = _context(org_a, actor_a)
    engine = create_engine(app_role_database_url)
    try:
        with pytest.raises(ActorNotInOrganizationError):
            with org_scoped_session(engine, context) as session:
                service = MemoryService(MemoryRepository(session))
                service.create_memory(
                    context,
                    owner_actor_id=foreign_owner,
                    kind="fact",
                    content={"claim": "x"},
                    visibility="private",
                )
    finally:
        engine.dispose()


def test_update_memory_supersedes_with_new_version(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-MEM-2: update writes a new version row and repoints the original."""
    organization_id = _seed_org(migrated_database_url)
    owner_id = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, owner_id)
    engine = create_engine(app_role_database_url)
    try:
        with org_scoped_session(engine, context) as session:
            service = MemoryService(MemoryRepository(session))
            original_id = service.create_memory(
                context,
                owner_actor_id=owner_id,
                kind="observation",
                content={"value": 1},
                visibility="private",
            )

        with org_scoped_session(engine, context) as session:
            service = MemoryService(MemoryRepository(session))
            successor_id = service.update_memory(context, original_id, content={"value": 2})

        assert successor_id != original_id

        with org_scoped_session(engine, context) as session:
            rows = session.execute(
                text("SELECT id, version, superseded_by, content FROM memory WHERE id IN (:a, :b)"),
                {"a": original_id, "b": successor_id},
            ).all()
        by_id = {row[0]: row for row in rows}

        # Two distinct rows exist: the original is retained (INV-MEM-2).
        assert len(by_id) == 2
        original = by_id[original_id]
        successor = by_id[successor_id]

        # JSONB is returned as a Python dict by psycopg (no manual decoding).
        assert original.version == 1
        assert original.superseded_by == successor_id
        assert original.content == {"value": 1}

        assert successor.version == 2
        assert successor.superseded_by is None
        assert successor.content == {"value": 2}
    finally:
        engine.dispose()


def test_update_already_superseded_memory_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-MEM-2: only the latest version of a memory may be superseded."""
    organization_id = _seed_org(migrated_database_url)
    owner_id = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, owner_id)
    engine = create_engine(app_role_database_url)
    try:
        with org_scoped_session(engine, context) as session:
            service = MemoryService(MemoryRepository(session))
            original_id = service.create_memory(
                context,
                owner_actor_id=owner_id,
                kind="observation",
                content={"value": 1},
                visibility="private",
            )
        with org_scoped_session(engine, context) as session:
            service = MemoryService(MemoryRepository(session))
            service.update_memory(context, original_id, content={"value": 2})

        with pytest.raises(MemoryAlreadySupersededError):
            with org_scoped_session(engine, context) as session:
                service = MemoryService(MemoryRepository(session))
                service.update_memory(context, original_id, content={"value": 3})
    finally:
        engine.dispose()
