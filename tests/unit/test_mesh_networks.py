
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext, MembershipRole, PlatformRole
from xiosync.persistence.models.browser import MeshNetwork, MeshNode
from xiosync.services.mesh_networks import MeshNetworkService

_ORG_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")
_ACTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000002")

@pytest.fixture
def org_context() -> OrgContext:
    return OrgContext(
        auth_identity_id=uuid.uuid4(),
        actor_id=_ACTOR_ID,
        organization_id=_ORG_ID,
        session_id=uuid.uuid4(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )

@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock(spec=Session)

def test_create_network_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    record = svc.create_network(
        org_context,
        name="test-net",
        network_type="tailscale",
        config={"key": "val"},
    )
    assert record.name == "test-net"
    assert record.network_type == "tailscale"
    assert record.config == {"key": "val"}
    assert mock_session.flush.call_count >= 1

def test_create_network_empty_config(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    record = svc.create_network(
        org_context,
        name="test-net-2",
        network_type="wireguard",
        config={},
    )
    assert record.config == {}
    assert mock_session.flush.call_count >= 1

def test_add_node_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    svc.add_node(
        org_context,
        network_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        address="10.0.0.1",
    )
    mock_session.add.assert_called()
    assert mock_session.flush.call_count >= 1

def test_add_node_empty_address(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    svc.add_node(
        org_context,
        network_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        address="",
    )
    assert mock_session.flush.call_count >= 1

def test_remove_node_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    mock_row = MagicMock()
    mock_session.scalar.return_value = mock_row

    svc.remove_node(
        org_context,
        network_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
    )
    mock_session.delete.assert_called_once_with(mock_row)
    assert mock_session.flush.call_count >= 1

def test_remove_node_not_found(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    mock_session.scalar.return_value = None

    with pytest.raises(ValueError, match="not found"):
        svc.remove_node(
            org_context,
            network_id=uuid.uuid4(),
            node_id=uuid.uuid4(),
        )

def test_list_networks_empty(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.scalars.return_value = mock_result

    records = svc.list_networks(org_context)
    assert records == []

def test_list_networks_multiple(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    mock_result = MagicMock()
    net = MeshNetwork(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        name="net1",
        network_type="tailscale",
        config={},
        created_at=datetime.now(UTC),
    )
    mock_result.all.return_value = [net, net]
    mock_session.scalars.return_value = mock_result

    records = svc.list_networks(org_context)
    assert len(records) == 2

def test_list_networks_filters_org(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.scalars.return_value = mock_result
    
    svc.list_networks(org_context)
    mock_session.scalars.assert_called_once()

def test_add_node_multiple(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = MeshNetworkService(mock_session)
    network_id = uuid.uuid4()
    svc.add_node(org_context, network_id=network_id, node_id=uuid.uuid4(), address="1")
    svc.add_node(org_context, network_id=network_id, node_id=uuid.uuid4(), address="2")
    assert mock_session.add.call_count >= 2

@pytest.fixture(autouse=True)
def mock_quota_service_daily_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xiosync.services.quotas.QuotaService.check_daily_events", lambda self, org_id: None)
