"""Integration tests for XIOGRID decoupling isolation in XIOSYNC.

Verifies on the real Alembic-migrated schema, as a plain application role:

- browser_pools, compute_runtimes, and mesh_networks tables are governed by
  org-scoped RLS — org_b cannot see org_a's rows.
- project_id scoping works for browser pools (project-level isolation).
- BrowserSession inherits project_id from its parent BrowserPool.
- ProjectService create/update/archive state transitions persist correctly.

Seeding uses the superuser (admin) connection; isolation assertions always use
the plain app-role connection via org_scoped_session.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.browser_pools import BrowserPoolService
from xiosync.services.compute_runtimes import ComputeRuntimeService
from xiosync.services.mesh_networks import MeshNetworkService
from xiosync.services.projects import ProjectNotFoundError, ProjectService

pytestmark = pytest.mark.integration


# ── Shared seed helpers ───────────────────────────────────────────────────────


def _seed_org_and_actor(
    admin_url: str,
    *,
    slug: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one active org + actor via superuser; return (org_id, actor_id)."""
    org_id = new_id()
    actor_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, :name, 'active')"
                ),
                {"id": org_id, "slug": slug, "name": slug},
            )
            conn.execute(
                text(
                    "INSERT INTO actors (id, organization_id, actor_type, state, "
                    "lifecycle_phase, trust_tier, health_status) "
                    "VALUES (:id, :org, 'human', 'active', 'operational', 'trusted', 'healthy')"
                ),
                {"id": actor_id, "org": org_id},
            )
    finally:
        engine.dispose()
    return org_id, actor_id


def _seed_project(
    admin_url: str,
    *,
    org_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    """Seed one project under org_id; return project_id."""
    project_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO projects (id, organization_id, name, slug, state) "
                    "VALUES (:id, :org, :name, :slug, 'active')"
                ),
                {"id": project_id, "org": org_id, "name": name, "slug": name},
            )
    finally:
        engine.dispose()
    return project_id


def _ctx(org_id: uuid.UUID, actor_id: uuid.UUID) -> OrgContext:
    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=actor_id,
        organization_id=org_id,
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. BrowserPool RLS
# ═══════════════════════════════════════════════════════════════════════════════


def test_browser_pool_rls_isolates_orgs(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """org_a's browser pool is invisible to org_b via RLS."""
    org_a, actor_a = _seed_org_and_actor(migrated_database_url, slug="bp-rls-org-a")
    org_b, actor_b = _seed_org_and_actor(migrated_database_url, slug="bp-rls-org-b")

    # Insert a pool for org_a via superuser (bypasses RLS deliberately).
    pool_id = new_id()
    admin_engine = create_engine(migrated_database_url)
    try:
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO browser_pools "
                    "(id, organization_id, name, engine_type, max_instances, stealth_config, state) "
                    "VALUES (:id, :org, 'pool-a', 'chromium', 5, '{}', 'active')"
                ),
                {"id": pool_id, "org": org_a},
            )
    finally:
        admin_engine.dispose()

    app_engine = create_engine(app_role_database_url)
    try:
        # org_a should see the pool
        with org_scoped_session(app_engine, _ctx(org_a, actor_a)) as session:
            rows = session.execute(
                text("SELECT id FROM browser_pools WHERE organization_id = :org"),
                {"org": org_a},
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == pool_id

        # org_b must see zero pools
        with org_scoped_session(app_engine, _ctx(org_b, actor_b)) as session:
            rows = session.execute(
                text("SELECT id FROM browser_pools"),
            ).fetchall()
            assert rows == [], f"RLS leak: org_b saw {rows}"
    finally:
        app_engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ComputeRuntime RLS
# ═══════════════════════════════════════════════════════════════════════════════


def test_compute_runtime_rls_isolates_orgs(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """org_a's compute runtime is invisible to org_b via RLS."""
    org_a, actor_a = _seed_org_and_actor(migrated_database_url, slug="cr-rls-org-a")
    org_b, actor_b = _seed_org_and_actor(migrated_database_url, slug="cr-rls-org-b")

    runtime_id = new_id()
    admin_engine = create_engine(migrated_database_url)
    try:
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO compute_runtimes "
                    "(id, organization_id, name, provider, config, state) "
                    "VALUES (:id, :org, 'runtime-a', 'aws', '{}', 'active')"
                ),
                {"id": runtime_id, "org": org_a},
            )
    finally:
        admin_engine.dispose()

    app_engine = create_engine(app_role_database_url)
    try:
        # org_a sees the runtime
        with org_scoped_session(app_engine, _ctx(org_a, actor_a)) as session:
            rows = session.execute(
                text("SELECT id FROM compute_runtimes"),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == runtime_id

        # org_b sees nothing
        with org_scoped_session(app_engine, _ctx(org_b, actor_b)) as session:
            rows = session.execute(
                text("SELECT id FROM compute_runtimes"),
            ).fetchall()
            assert rows == [], f"RLS leak: org_b saw {rows}"
    finally:
        app_engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. MeshNetwork RLS
# ═══════════════════════════════════════════════════════════════════════════════


def test_mesh_network_rls_isolates_orgs(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """org_a's mesh network is invisible to org_b via RLS."""
    org_a, actor_a = _seed_org_and_actor(migrated_database_url, slug="mn-rls-org-a")
    org_b, actor_b = _seed_org_and_actor(migrated_database_url, slug="mn-rls-org-b")

    network_id = new_id()
    admin_engine = create_engine(migrated_database_url)
    try:
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO mesh_networks "
                    "(id, organization_id, name, network_type, config, state) "
                    "VALUES (:id, :org, 'net-a', 'tailscale', '{}', 'active')"
                ),
                {"id": network_id, "org": org_a},
            )
    finally:
        admin_engine.dispose()

    app_engine = create_engine(app_role_database_url)
    try:
        # org_a sees the network
        with org_scoped_session(app_engine, _ctx(org_a, actor_a)) as session:
            rows = session.execute(
                text("SELECT id FROM mesh_networks"),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == network_id

        # org_b sees nothing
        with org_scoped_session(app_engine, _ctx(org_b, actor_b)) as session:
            rows = session.execute(
                text("SELECT id FROM mesh_networks"),
            ).fetchall()
            assert rows == [], f"RLS leak: org_b saw {rows}"
    finally:
        app_engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Project-scoped pool query
# ═══════════════════════════════════════════════════════════════════════════════


def test_project_scoped_pool_query(migrated_database_url: str) -> None:
    """A BrowserPool associated with proj_a is not visible under proj_b's filter."""
    org_id, actor_id = _seed_org_and_actor(migrated_database_url, slug="proj-pool-org")
    proj_a = _seed_project(migrated_database_url, org_id=org_id, name="proj-pool-a")
    proj_b = _seed_project(migrated_database_url, org_id=org_id, name="proj-pool-b")

    pool_id = new_id()
    admin_engine = create_engine(migrated_database_url)
    try:
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO browser_pools "
                    "(id, organization_id, project_id, name, engine_type, max_instances, stealth_config, state) "
                    "VALUES (:id, :org, :proj, 'scoped-pool', 'chromium', 3, '{}', 'active')"
                ),
                {"id": pool_id, "org": org_id, "proj": proj_a},
            )
    finally:
        admin_engine.dispose()

    ctx = _ctx(org_id, actor_id)
    engine = create_engine(migrated_database_url)
    try:
        with org_scoped_session(engine, ctx) as session:
            # proj_a query -> should find the pool
            rows_a = session.execute(
                text("SELECT id FROM browser_pools WHERE project_id = :proj"),
                {"proj": proj_a},
            ).fetchall()
            assert len(rows_a) == 1, f"Expected 1 pool for proj_a, got {len(rows_a)}"
            assert rows_a[0][0] == pool_id

            # proj_b query -> should find nothing
            rows_b = session.execute(
                text("SELECT id FROM browser_pools WHERE project_id = :proj"),
                {"proj": proj_b},
            ).fetchall()
            assert rows_b == [], f"Expected 0 pools for proj_b, got {rows_b}"
    finally:
        engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. BrowserSession inherits project_id from pool
# ═══════════════════════════════════════════════════════════════════════════════


def test_browser_session_inherits_project_from_pool(migrated_database_url: str) -> None:
    """A BrowserSession created under a project-scoped pool carries that project_id."""
    org_id, actor_id = _seed_org_and_actor(migrated_database_url, slug="bs-inherit-org")
    proj_id = _seed_project(migrated_database_url, org_id=org_id, name="bs-inherit-proj")

    pool_id = new_id()
    sess_id = new_id()

    admin_engine = create_engine(migrated_database_url)
    try:
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO browser_pools "
                    "(id, organization_id, project_id, name, engine_type, max_instances, stealth_config, state) "
                    "VALUES (:id, :org, :proj, 'inherit-pool', 'chromium', 2, '{}', 'active')"
                ),
                {"id": pool_id, "org": org_id, "proj": proj_id},
            )
            # Insert a BrowserSession referencing the pool, propagating project_id
            conn.execute(
                text(
                    "INSERT INTO browser_sessions "
                    "(id, organization_id, project_id, pool_id, session_data, state) "
                    "VALUES (:id, :org, :proj, :pool, '{}', 'initializing')"
                ),
                {"id": sess_id, "org": org_id, "proj": proj_id, "pool": pool_id},
            )

        # Verify the stored project_id
        with admin_engine.connect() as conn:
            row = conn.execute(
                text("SELECT project_id FROM browser_sessions WHERE id = :id"),
                {"id": sess_id},
            ).fetchone()
            assert row is not None, "BrowserSession row not found"
            assert row[0] == proj_id, (
                f"Expected project_id={proj_id}, got {row[0]}"
            )
    finally:
        admin_engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Project lifecycle via ProjectService
# ═══════════════════════════════════════════════════════════════════════════════


def test_project_lifecycle(migrated_database_url: str) -> None:
    """ProjectService create -> update -> archive state transitions are correct."""
    org_id, actor_id = _seed_org_and_actor(migrated_database_url, slug="proj-lifecycle-org")
    ctx = _ctx(org_id, actor_id)
    engine = create_engine(migrated_database_url)
    project_id: uuid.UUID

    try:
        # ── create ──────────────────────────────────────────────────────────
        with org_scoped_session(engine, ctx) as session:
            svc = ProjectService(session)
            rec = svc.create_project(
                ctx,
                name="Lifecycle Project",
                slug="lifecycle-project",
                description="Initial description",
                config={"tier": "free"},
            )
            project_id = rec.id
            assert rec.name == "Lifecycle Project"
            assert rec.slug == "lifecycle-project"
            assert rec.state == "active"
            assert rec.description == "Initial description"
            assert rec.config == {"tier": "free"}

        # ── update ──────────────────────────────────────────────────────────
        with org_scoped_session(engine, ctx) as session:
            svc = ProjectService(session)
            updated = svc.update_project(
                ctx,
                project_id,
                name="Lifecycle Project v2",
                description="Updated description",
                config={"tier": "pro"},
            )
            assert updated.name == "Lifecycle Project v2"
            assert updated.description == "Updated description"
            assert updated.config == {"tier": "pro"}
            assert updated.state == "active"
            assert updated.updated_at is not None

        # ── get (verify persistence across sessions) ─────────────────────
        with org_scoped_session(engine, ctx) as session:
            svc = ProjectService(session)
            fetched = svc.get_project(ctx, project_id)
            assert fetched.name == "Lifecycle Project v2"
            assert fetched.state == "active"

        # ── archive ──────────────────────────────────────────────────────
        with org_scoped_session(engine, ctx) as session:
            svc = ProjectService(session)
            archived = svc.archive_project(ctx, project_id)
            assert archived.state == "archived"

        # ── idempotent archive ────────────────────────────────────────────
        with org_scoped_session(engine, ctx) as session:
            svc = ProjectService(session)
            archived_again = svc.archive_project(ctx, project_id)
            assert archived_again.state == "archived"

        # ── get_project on unknown id raises ProjectNotFoundError ─────────
        with org_scoped_session(engine, ctx) as session:
            svc = ProjectService(session)
            bogus_id = new_id()
            with pytest.raises(ProjectNotFoundError):
                svc.get_project(ctx, bogus_id)
    finally:
        engine.dispose()
