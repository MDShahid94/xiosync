"""Unit tests for BrowserPoolService."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext, MembershipRole, PlatformRole
from xiosync.persistence.models.browser import BrowserPool
from xiosync.services.browser_pools import (
    BrowserPoolNotFoundError,
    BrowserPoolService,
)

_ORG_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")
_ACTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000002")
_IDENTITY_ID = uuid.UUID("00000000-0000-7000-8000-aaaaaaaaaaaa")
_SESSION_ID = uuid.UUID("00000000-0000-7000-8000-bbbbbbbbbbbb")

@pytest.fixture
def org_context() -> OrgContext:
    return OrgContext(
        auth_identity_id=_IDENTITY_ID,
        actor_id=_ACTOR_ID,
        organization_id=_ORG_ID,
        session_id=_SESSION_ID,
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )

@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock(spec=Session)

def test_create_pool_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    record = svc.create_pool(
        org_context,
        name="test-pool",
        engine_type="chromium",
        max_instances=5,
        config={"key": "val"},
    )
    assert record.name == "test-pool"
    assert record.engine_type == "chromium"
    assert record.max_instances == 5
    assert record.stealth_config == {"key": "val"}
    assert record.organization_id == _ORG_ID
    assert record.state == "active"
    assert mock_session.add.call_count >= 3  # Pool, Operation, Event
    mock_session.flush.assert_called_once()

def test_create_pool_invalid_engine(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    with pytest.raises(ValueError, match="Unsupported engine type"):
        svc.create_pool(
            org_context,
            name="test-pool",
            engine_type="invalid",
            max_instances=5,
        )

def test_get_pool_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    pool_id = uuid.uuid4()
    mock_pool = BrowserPool(
        id=pool_id,
        organization_id=_ORG_ID,
        name="test",
        engine_type="chrome",
        max_instances=2,
        stealth_config={},
        state="active",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = mock_pool

    record = svc.get_pool(org_context, pool_id)
    assert record.id == pool_id
    assert record.name == "test"

def test_get_pool_not_found(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    mock_session.scalar.return_value = None
    with pytest.raises(BrowserPoolNotFoundError):
        svc.get_pool(org_context, uuid.uuid4())

def test_list_pools_empty(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.scalars.return_value = mock_result

    records = svc.list_pools(org_context)
    assert records == []

def test_list_pools_multiple(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    mock_result = MagicMock()
    mock_pool = BrowserPool(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        name="test",
        engine_type="chrome",
        max_instances=2,
        stealth_config={},
        state="active",
        created_at=datetime.now(UTC),
    )
    mock_result.all.return_value = [mock_pool, mock_pool]
    mock_session.scalars.return_value = mock_result

    records = svc.list_pools(org_context)
    assert len(records) == 2

def test_scale_pool_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    pool_id = uuid.uuid4()
    mock_pool = BrowserPool(
        id=pool_id,
        organization_id=_ORG_ID,
        name="test",
        engine_type="chrome",
        max_instances=2,
        stealth_config={},
        state="active",
        created_at=datetime.now(UTC),
    )
    # scalar is called twice: first to get pool, second to return fresh pool
    mock_session.scalar.side_effect = [mock_pool, mock_pool]

    record = svc.scale_pool(org_context, pool_id, target_instances=10)
    assert record.id == pool_id
    mock_session.execute.assert_called_once()
    mock_session.flush.assert_called_once()

def test_scale_pool_not_found(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    mock_session.scalar.return_value = None
    with pytest.raises(BrowserPoolNotFoundError):
        svc.scale_pool(org_context, uuid.uuid4(), target_instances=10)

def test_destroy_pool_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    pool_id = uuid.uuid4()
    mock_pool = BrowserPool(
        id=pool_id,
        organization_id=_ORG_ID,
        name="test",
        engine_type="chrome",
        max_instances=2,
        stealth_config={},
        state="active",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = mock_pool

    svc.destroy_pool(org_context, pool_id)
    mock_session.execute.assert_called_once()
    mock_session.flush.assert_called_once()

def test_destroy_pool_not_found(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserPoolService(mock_session)
    mock_session.scalar.return_value = None
    with pytest.raises(BrowserPoolNotFoundError):
        svc.destroy_pool(org_context, uuid.uuid4())

@pytest.fixture(autouse=True)
def mock_quota_service_daily_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xiosync.services.quotas.QuotaService.check_daily_events", lambda self, org_id: None)
