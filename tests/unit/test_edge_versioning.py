"""Unit tests for edge versioning (superseded_by pattern, O-3)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.platform.ids import new_id
from xiosync.services.ontology import EdgeService


def _dummy_context() -> OrgContext:
    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=new_id(),
        organization_id=new_id(),
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )


def test_edge_service_update_edge_success() -> None:
    repo = MagicMock()
    context = _dummy_context()
    old_edge_id = new_id()
    new_edge_id = new_id()
    source_id = new_id()
    target_id = new_id()

    old_edge = MagicMock()
    old_edge.source_id = source_id
    old_edge.target_id = target_id
    old_edge.edge_type = "reports_to"
    old_edge.graph_class = "hierarchy"
    old_edge.weight = 1.0
    old_edge.state = "active"
    old_edge.properties = {"dept": "eng"}
    old_edge.version = 1

    repo.get_edge.return_value = old_edge
    repo.insert_edge.return_value = new_edge_id

    service = EdgeService(repo)
    result_id = service.update_edge(
        context,
        edge_id=old_edge_id,
        weight=2.5,
        properties={"dept": "product"},
    )

    assert result_id == new_edge_id
    repo.get_edge.assert_called_once_with(context, old_edge_id)
    repo.insert_edge.assert_called_once_with(
        context,
        source_id=source_id,
        target_id=target_id,
        edge_type="reports_to",
        graph_class="hierarchy",
        weight=2.5,
        state="active",
        properties={"dept": "product"},
        version=2,
        superseded_by=None,
    )
    repo.supersede_edge.assert_called_once_with(context, old_edge_id, new_edge_id)


def test_edge_service_update_edge_not_found() -> None:
    repo = MagicMock()
    context = _dummy_context()
    edge_id = new_id()
    repo.get_edge.return_value = None

    service = EdgeService(repo)
    with pytest.raises(ValueError, match=f"Edge {edge_id} not found"):
        service.update_edge(context, edge_id=edge_id)


def test_update_edge_api_endpoint() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import patch
    from xiosync.api.routers.ontology import router

    app = FastAPI()
    app.include_router(router)

    edge_id = new_id()
    new_edge_id = new_id()
    context = _dummy_context()

    @app.middleware("http")
    async def inject_state(request, call_next):
        request.state.org_context = context
        request.state.org_session = MagicMock()
        return await call_next(request)

    client = TestClient(app)

    with patch("xiosync.services.ontology.EdgeService.update_edge") as mock_update:
        mock_update.return_value = new_edge_id
        resp = client.put(
            f"/edges/{edge_id}",
            json={"weight": 1.5, "properties": {"color": "blue"}},
        )
        assert resp.status_code == 201
        assert resp.json() == {"id": str(new_edge_id)}
        mock_update.assert_called_once_with(
            context,
            edge_id=edge_id,
            edge_type=None,
            weight=1.5,
            state=None,
            properties={"color": "blue"},
        )

    with patch("xiosync.services.ontology.EdgeService.update_edge") as mock_update:
        mock_update.side_effect = ValueError("Edge not found")
        resp = client.put(
            f"/edges/{edge_id}",
            json={"weight": 1.5},
        )
        assert resp.status_code == 422
        assert resp.json()["title"] == "Edge update failed"

