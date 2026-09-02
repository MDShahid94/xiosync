
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext, MembershipRole, PlatformRole
from xiosync.persistence.models.browser import RuntimeNode, ComputeRuntime as RuntimeProvider
from xiosync.services.compute_runtimes import ComputeRuntimeService

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

def test_register_provider_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    record = svc.register_provider(
        org_context,
        name="test-provider",
        provider="aws",
        config={"region": "us-east-1"},
    )
    assert record.name == "test-provider"
    assert record.provider == "aws"
    assert record.config == {"region": "us-east-1"}
    mock_session.add.assert_called()
    assert mock_session.flush.call_count >= 1

def test_register_provider_kubernetes(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    record = svc.register_provider(
        org_context,
        name="k8s-prov",
        provider="kubernetes",
        config={"namespace": "default"},
    )
    assert record.provider == "kubernetes"

def test_provision_node_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    runtime_id = uuid.uuid4()
    record = svc.provision_node(
        org_context,
        runtime_id=runtime_id,
        spec={"cpu": "2", "memory": "4Gi"},
    )
    assert record.runtime_id == runtime_id
    assert record.state == "provisioning"
    assert record.spec == {"cpu": "2", "memory": "4Gi"}

def test_provision_node_empty_spec(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    record = svc.provision_node(
        org_context,
        runtime_id=uuid.uuid4(),
        spec={},
    )
    assert record.spec == {}

def test_list_nodes_empty(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.scalars.return_value = mock_result

    records = svc.list_nodes(org_context, runtime_id=uuid.uuid4())
    assert records == []

def test_list_nodes_filtered(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    mock_result = MagicMock()
    node = RuntimeNode(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        runtime_id=uuid.uuid4(),
        state="running",
        node_metadata={}, hostname="test",
        created_at=datetime.now(UTC),
    )
    mock_result.all.return_value = [node]
    mock_session.scalars.return_value = mock_result

    records = svc.list_nodes(org_context, runtime_id=node.runtime_id, state="running")
    assert len(records) == 1

def test_terminate_node_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    node_id = uuid.uuid4()
    mock_row = MagicMock()
    mock_row.state = "running"
    mock_session.scalar.return_value = mock_row

    svc.terminate_node(org_context, node_id)
    assert mock_row.state == "terminated"
    assert mock_session.flush.call_count >= 1

def test_terminate_node_not_found(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    mock_session.scalar.return_value = None
    with pytest.raises(ValueError, match="not found"):
        svc.terminate_node(org_context, uuid.uuid4())

def test_get_node_health_running(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    node = RuntimeNode(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        runtime_id=uuid.uuid4(),
        state="running",
        node_metadata={}, hostname="test",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = node

    record = svc.get_node_health(org_context, node.id)
    assert record.status == "healthy"
    assert record.last_heartbeat is not None

def test_get_node_health_not_running(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    node = RuntimeNode(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        runtime_id=uuid.uuid4(),
        state="provisioning",
        node_metadata={}, hostname="test",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = node

    record = svc.get_node_health(org_context, node.id)
    assert record.status == "unknown"
    assert record.last_heartbeat is None

def test_get_node_health_not_found(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = ComputeRuntimeService(mock_session)
    mock_session.scalar.return_value = None
    with pytest.raises(ValueError, match="not found"):
        svc.get_node_health(org_context, uuid.uuid4())

@pytest.fixture(autouse=True)
def mock_quota_service_daily_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xiosync.services.quotas.QuotaService.check_daily_events", lambda self, org_id: None)
