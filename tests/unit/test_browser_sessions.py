"""Unit tests for BrowserSessionService."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext, MembershipRole, PlatformRole
from xiosync.persistence.models.browser import BrowserSession
from xiosync.services.browser_sessions import (
    BrowserSessionNotFoundError,
    BrowserSessionService,
)

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

def test_create_session_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    pool_id = uuid.uuid4()
    record = svc.create_session(
        org_context,
        pool_id=pool_id,
        config={"key": "val"},
    )
    assert record.pool_id == pool_id
    assert record.session_data == {"key": "val"}
    assert record.organization_id == _ORG_ID
    assert record.state == "initializing"
    assert mock_session.add.call_count >= 3
    mock_session.flush.assert_called_once()

def test_create_session_empty_config(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    pool_id = uuid.uuid4()
    record = svc.create_session(
        org_context,
        pool_id=pool_id,
        config=None,
    )
    assert record.session_data == {}

def test_verify_session_success_active(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    session_id = uuid.uuid4()
    mock_row = BrowserSession(
        id=session_id,
        organization_id=_ORG_ID,
        pool_id=uuid.uuid4(),
        session_data={},
        state="active",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = mock_row

    record = svc.verify_session(org_context, session_id)
    assert record.session_id == session_id
    assert record.status == "healthy"

def test_verify_session_success_failed(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    session_id = uuid.uuid4()
    mock_row = BrowserSession(
        id=session_id,
        organization_id=_ORG_ID,
        pool_id=uuid.uuid4(),
        session_data={},
        state="failed",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = mock_row

    record = svc.verify_session(org_context, session_id)
    assert record.status == "immediate"

def test_verify_session_success_suspended(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    session_id = uuid.uuid4()
    mock_row = BrowserSession(
        id=session_id,
        organization_id=_ORG_ID,
        pool_id=uuid.uuid4(),
        session_data={},
        state="suspended",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = mock_row

    record = svc.verify_session(org_context, session_id)
    assert record.status == "soon"

def test_verify_session_not_found(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    mock_session.scalar.return_value = None
    with pytest.raises(BrowserSessionNotFoundError):
        svc.verify_session(org_context, uuid.uuid4())

def test_terminate_session_success(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    session_id = uuid.uuid4()
    mock_row = BrowserSession(
        id=session_id,
        organization_id=_ORG_ID,
        pool_id=uuid.uuid4(),
        session_data={},
        state="active",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = mock_row

    svc.terminate_session(org_context, session_id)
    mock_session.execute.assert_called_once()
    mock_session.flush.assert_called_once()

def test_terminate_session_not_found(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    mock_session.scalar.return_value = None
    with pytest.raises(BrowserSessionNotFoundError):
        svc.terminate_session(org_context, uuid.uuid4())

def test_list_sessions_all(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    mock_result = MagicMock()
    mock_row = BrowserSession(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        pool_id=uuid.uuid4(),
        session_data={},
        state="active",
        created_at=datetime.now(UTC),
    )
    mock_result.all.return_value = [mock_row]
    mock_session.scalars.return_value = mock_result

    records = svc.list_sessions(org_context)
    assert len(records) == 1

def test_list_sessions_filtered(org_context: OrgContext, mock_session: MagicMock) -> None:
    svc = BrowserSessionService(mock_session)
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.scalars.return_value = mock_result

    records = svc.list_sessions(org_context, pool_id=uuid.uuid4(), state="active")
    assert records == []

@pytest.fixture(autouse=True)
def mock_quota_service_daily_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xiosync.services.quotas.QuotaService.check_daily_events", lambda self, org_id: None)
