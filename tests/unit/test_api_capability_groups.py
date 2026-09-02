"""Unit tests for the capability group management API router (improvement #7).

Tests:
- GET /api/v1/capability-groups
- GET /api/v1/capability-groups/{name} (org override)
- GET /api/v1/capability-groups/{name} (global fallback)
- GET /api/v1/capability-groups/{name} (not found -> 404)
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from xiosync.api.routers.capability_groups import router as capability_groups_router
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.models.registry import CapabilityGroup
from xiosync.platform.ids import new_id

_ORG_ID = new_id()
_ACTOR_ID = new_id()

_CONTEXT = OrgContext(
    auth_identity_id=new_id(),
    actor_id=_ACTOR_ID,
    organization_id=_ORG_ID,
    session_id=new_id(),
    platform_role=PlatformRole.NONE,
    membership_role=MembershipRole.ORG_ADMIN,
)


def _make_app(mock_session: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(capability_groups_router, prefix="/api/v1")

    @app.middleware("http")
    async def inject_state(request: Request, call_next: Any) -> Any:
        request.state.org_context = _CONTEXT
        request.state.org_session = mock_session
        return await call_next(request)

    return app


class TestCapabilityGroupsAPI:
    def test_list_capability_groups(self) -> None:
        mock_session = MagicMock()
        group1 = CapabilityGroup(
            id=new_id(),
            organization_id=None,
            name="readonly",
            description="Read-only access",
            operations=["event.read"],
            state="active",
        )
        group2 = CapabilityGroup(
            id=new_id(),
            organization_id=_ORG_ID,
            name="workflow.manage",
            description="Manage workflows",
            operations=["workflow.create", "workflow.read"],
            state="active",
        )
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [group1, group2]
        mock_session.execute.return_value.scalars.return_value = mock_scalars

        app = _make_app(mock_session)
        client = TestClient(app)

        resp = client.get("/api/v1/capability-groups")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["name"] == "readonly"
        assert data[0]["organization_id"] is None
        assert data[0]["operations"] == ["event.read"]
        assert data[0]["state"] == "active"
        assert data[1]["name"] == "workflow.manage"
        assert data[1]["organization_id"] == str(_ORG_ID)

    def test_get_capability_group_org_override(self) -> None:
        mock_session = MagicMock()
        group = CapabilityGroup(
            id=new_id(),
            organization_id=_ORG_ID,
            name="workflow.manage",
            description="Org custom workflow manage",
            operations=["workflow.create", "workflow.delete"],
            state="active",
        )
        mock_session.scalar.return_value = group

        app = _make_app(mock_session)
        client = TestClient(app)

        resp = client.get("/api/v1/capability-groups/workflow.manage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(group.id)
        assert data["name"] == "workflow.manage"
        assert data["description"] == "Org custom workflow manage"
        assert data["operations"] == ["workflow.create", "workflow.delete"]
        assert data["state"] == "active"
        assert data["organization_id"] == str(_ORG_ID)

    def test_get_capability_group_global_fallback(self) -> None:
        mock_session = MagicMock()
        group = CapabilityGroup(
            id=new_id(),
            organization_id=None,
            name="readonly",
            description="Global readonly",
            operations=["event.read"],
            state="active",
        )
        # First scalar call (org lookup) returns None, second (global lookup) returns group
        mock_session.scalar.side_effect = [None, group]

        app = _make_app(mock_session)
        client = TestClient(app)

        resp = client.get("/api/v1/capability-groups/readonly")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(group.id)
        assert data["name"] == "readonly"
        assert data["organization_id"] is None
        assert data["operations"] == ["event.read"]

    def test_get_capability_group_not_found(self) -> None:
        mock_session = MagicMock()
        mock_session.scalar.side_effect = [None, None]

        app = _make_app(mock_session)
        client = TestClient(app)

        resp = client.get("/api/v1/capability-groups/nonexistent")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Group 'nonexistent' not found"}
