import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from xiosync.api.app import create_app
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.platform.clock import FixedClock

pytestmark = pytest.mark.integration


class _FakeSessionService:
    def __init__(self, context: OrgContext) -> None:
        self.context = context

    def validate_access_token(self, token: str, now: Any = None) -> OrgContext:
        return self.context


def _seed_org_actor(admin_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    engine = create_engine(admin_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO organizations (id, slug, name, state) "
                "VALUES (:id, :slug, 'Org', 'active')"
            ),
            {"id": org_id, "slug": f"org-{org_id}"},
        )
        conn.execute(
            text(
                "INSERT INTO actors (id, organization_id, actor_type, state, "
                "lifecycle_phase, trust_tier, health_status) VALUES "
                "(:id, :org, 'human', 'active', 'operational', 'trusted', 'healthy')"
            ),
            {"id": actor_id, "org": org_id},
        )
    engine.dispose()
    return org_id, actor_id


@pytest.fixture
def org_actor_seeded(migrated_database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    return _seed_org_actor(migrated_database_url)


@pytest.fixture
def api_client(
    app_role_database_url: str,
    org_actor_seeded: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    org_id, actor_id = org_actor_seeded
    context = OrgContext(
        auth_identity_id=uuid.uuid4(),
        actor_id=actor_id,
        organization_id=org_id,
        session_id=uuid.uuid4(),
        platform_role=PlatformRole.PLATFORM_ADMIN,
        membership_role=MembershipRole.ORG_ADMIN,
    )

    @contextmanager
    def fake_scope(engine: Any, ctx: OrgContext) -> Iterator[Any]:
        from sqlalchemy.orm import Session as OrmSession

        eng = create_engine(app_role_database_url)
        with OrmSession(eng) as session, session.begin():
            # set rls tenant
            session.execute(
                text("SELECT set_config('app.current_org', :org_id, true)"),
                {"org_id": str(ctx.organization_id)},
            )
            yield session
        eng.dispose()

    monkeypatch.setattr("xiosync.api.middleware.org_scoped_session", fake_scope)

    app = create_app(
        session_service=_FakeSessionService(context),
        engine=object(),
        clock=FixedClock(datetime(2026, 7, 18, 12, tzinfo=UTC)),
        max_body_bytes=256,
    )
    return TestClient(app)


def test_organization_branding_flow(
    api_client: TestClient, org_actor_seeded: tuple[uuid.UUID, uuid.UUID]
) -> None:
    org_id, _ = org_actor_seeded

    # 1. Get branding (should be 404 initially)
    resp = api_client.get(
        f"/api/v1/organizations/{org_id}/branding", headers={"Authorization": "Bearer fake"}
    )
    assert resp.status_code == 404

    # 2. Update branding (Create)
    resp = api_client.put(
        f"/api/v1/organizations/{org_id}/branding",
        headers={"Authorization": "Bearer fake"},
        json={
            "theme_mode": "dark",
            "primary_color": "#000000",
            "logo_url": "https://example.com/logo.png",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["organization_id"] == str(org_id)
    assert data["theme_mode"] == "dark"
    assert data["primary_color"] == "#000000"
    assert data["logo_url"] == "https://example.com/logo.png"
    assert data["custom_domain"] is None

    # 3. Get branding (should now exist)
    resp = api_client.get(
        f"/api/v1/organizations/{org_id}/branding", headers={"Authorization": "Bearer fake"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["theme_mode"] == "dark"

    # 4. Update branding (Update)
    resp = api_client.put(
        f"/api/v1/organizations/{org_id}/branding",
        headers={"Authorization": "Bearer fake"},
        json={"theme_mode": "light", "custom_domain": "app.xiosync.dev"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["theme_mode"] == "light"
    # Wait, the PUT endpoint does a partial update based on the payload.
    # Actually my implementation only updates provided fields.
    assert data["custom_domain"] == "app.xiosync.dev"
