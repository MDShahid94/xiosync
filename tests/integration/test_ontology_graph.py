"""Real-PostgreSQL tests for graph classes & acyclicity (H5; doc 03 §3).

These focus specifically on the four graph classes and the acyclicity rule the
``EdgeService`` enforces on write, complementing ``test_ontology.py`` (which
covers the broader Edge/Memory surface). Every service call runs as the plain
application role inside the canonical ``org_scoped_session`` RLS gate; seeding
uses the admin (superuser) connection.

They prove, on the real schema:

* the four graph classes ``hierarchy``, ``workflow``, ``relationship`` and
  ``dependency`` are the closed, enforced set — an unknown class is rejected
  with a domain error before any insert (doc 03 §3);
* acyclicity is strictly enforced on write for the acyclic classes — a direct
  cycle, a transitive (multi-hop) cycle, and a self-loop are all rejected
  (INV-EDGE-2), while the ``relationship`` class freely permits cycles; and
* cross-organization edges are forbidden in every graph class (INV-EDGE-1),
  rejected at the service boundary before the composite FK would fire.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.domain.ontology import (
    GRAPH_CLASSES,
    GraphCycleError,
    UnknownGraphClassError,
)
from xiosync.persistence.ontology import EdgeRepository
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.ontology import ActorNotInOrganizationError, EdgeService

pytestmark = pytest.mark.integration


def _seed_org(admin_url: str) -> uuid.UUID:
    organization_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'Graph Test', 'active')"
                ),
                {"id": organization_id, "slug": f"graph-{organization_id}"},
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


def _edge_count(engine: Engine, context: OrgContext, edge_id: uuid.UUID) -> int:
    with org_scoped_session(engine, context) as session:
        return int(
            session.execute(
                text("SELECT count(*) FROM edges WHERE id = :id"), {"id": edge_id}
            ).scalar_one()
        )


# -- The closed set of graph classes ---------------------------------------


def test_four_graph_classes_are_the_closed_set() -> None:
    """doc 03 §3: exactly four graph classes exist, no more, no fewer."""
    assert GRAPH_CLASSES == {"hierarchy", "workflow", "relationship", "dependency"}


def test_unknown_graph_class_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """An edge outside the four classes is rejected before any insert."""
    organization_id = _seed_org(migrated_database_url)
    source_id = _seed_actor(migrated_database_url, organization_id)
    target_id = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, source_id)
    engine = create_engine(app_role_database_url)
    try:
        with pytest.raises(UnknownGraphClassError):
            _create_edge(
                engine,
                context,
                source_id=source_id,
                target_id=target_id,
                graph_class="social",
            )
        # Nothing was written for the rejected class.
        with org_scoped_session(engine, context) as session:
            total = session.execute(text("SELECT count(*) FROM edges")).scalar_one()
        assert total == 0
    finally:
        engine.dispose()


# -- Acyclicity strictly enforced on write ---------------------------------


@pytest.mark.parametrize("graph_class", ["hierarchy", "workflow"])
def test_direct_cycle_is_rejected(
    graph_class: str, migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EDGE-2: a two-node back-edge is rejected in an acyclic class."""
    organization_id = _seed_org(migrated_database_url)
    x = _seed_actor(migrated_database_url, organization_id)
    y = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, x)
    engine = create_engine(app_role_database_url)
    try:
        _create_edge(engine, context, source_id=x, target_id=y, graph_class=graph_class)
        with pytest.raises(GraphCycleError):
            _create_edge(engine, context, source_id=y, target_id=x, graph_class=graph_class)
    finally:
        engine.dispose()


@pytest.mark.parametrize("graph_class", ["hierarchy", "workflow", "dependency"])
def test_transitive_cycle_is_rejected(
    graph_class: str, migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EDGE-2: a multi-hop back-edge (a→b→c→a) is rejected."""
    organization_id = _seed_org(migrated_database_url)
    a = _seed_actor(migrated_database_url, organization_id)
    b = _seed_actor(migrated_database_url, organization_id)
    c = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, a)
    engine = create_engine(app_role_database_url)
    try:
        _create_edge(engine, context, source_id=a, target_id=b, graph_class=graph_class)
        _create_edge(engine, context, source_id=b, target_id=c, graph_class=graph_class)
        with pytest.raises(GraphCycleError):
            _create_edge(engine, context, source_id=c, target_id=a, graph_class=graph_class)
    finally:
        engine.dispose()


@pytest.mark.parametrize("graph_class", ["hierarchy", "workflow", "dependency"])
def test_self_loop_is_rejected(
    graph_class: str, migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EDGE-2: the degenerate self-loop (n→n) is a cycle and is rejected."""
    organization_id = _seed_org(migrated_database_url)
    n = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, n)
    engine = create_engine(app_role_database_url)
    try:
        with pytest.raises(GraphCycleError):
            _create_edge(engine, context, source_id=n, target_id=n, graph_class=graph_class)
    finally:
        engine.dispose()


def test_acyclic_diamond_is_allowed(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """A DAG with converging paths (a→b, a→c, b→d, c→d) is not a cycle."""
    organization_id = _seed_org(migrated_database_url)
    a = _seed_actor(migrated_database_url, organization_id)
    b = _seed_actor(migrated_database_url, organization_id)
    c = _seed_actor(migrated_database_url, organization_id)
    d = _seed_actor(migrated_database_url, organization_id)
    context = _context(organization_id, a)
    engine = create_engine(app_role_database_url)
    try:
        _create_edge(engine, context, source_id=a, target_id=b, graph_class="workflow")
        _create_edge(engine, context, source_id=a, target_id=c, graph_class="workflow")
        _create_edge(engine, context, source_id=b, target_id=d, graph_class="workflow")
        closing = _create_edge(engine, context, source_id=c, target_id=d, graph_class="workflow")
        assert _edge_count(engine, context, closing) == 1
    finally:
        engine.dispose()


def test_relationship_class_permits_cycles(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """doc 03 §3: the ``relationship`` class is not acyclic; cycles are legal."""
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
        closing = _create_edge(
            engine,
            context,
            source_id=y,
            target_id=x,
            graph_class="relationship",
            edge_type="collaborates_with",
        )
        assert _edge_count(engine, context, closing) == 1
    finally:
        engine.dispose()


# -- Cross-organization edges forbidden in every class ---------------------


@pytest.mark.parametrize(
    ("graph_class", "edge_type"),
    [
        ("hierarchy", "manages"),
        ("workflow", "delegates_to"),
        ("dependency", "owns"),
        ("relationship", "collaborates_with"),
    ],
)
def test_cross_org_edge_rejected_in_every_class(
    graph_class: str,
    edge_type: str,
    migrated_database_url: str,
    app_role_database_url: str,
) -> None:
    """INV-EDGE-1: a target in another org is rejected regardless of class."""
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
                graph_class=graph_class,
                edge_type=edge_type,
            )
    finally:
        engine.dispose()
