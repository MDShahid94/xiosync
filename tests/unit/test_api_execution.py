"""Unit tests for the execution-plane and DLQ governance API routers.

Tests the HTTP contracts of:
- ``POST /api/v1/execution/tasks/{task_id}/lease``      (INV-EXEC-1)
- ``POST /api/v1/execution/tasks/{task_id}/heartbeat``
- ``POST /api/v1/execution/tasks/{task_id}/complete``   (INV-EXEC-2)
- ``GET  /api/v1/dlq/{dead_letter_id}``
- ``POST /api/v1/dlq/{dead_letter_id}/propose``          (INV-DLQ-2)
- ``POST /api/v1/dlq/{dead_letter_id}/resolve``          (INV-DLQ-3)

These are router-layer unit tests — no real database, no real migrations. The
``WorkflowService`` is replaced by a ``MagicMock`` (INV-ROADMAP-3: using
``unittest.mock.MagicMock`` in test files is explicitly authorised). The
``AuthenticationMiddleware`` is bypassed by monkeypatching
``org_scoped_session`` and injecting ``org_context`` + ``org_session`` onto the
ASGI scope's state dict via a thin pass-through middleware.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from xiosync.api.routers.dlq import router as dlq_router
from xiosync.api.routers.execution import router as execution_router
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.platform.ids import new_id
from xiosync.platform.task_credentials import (
    TaskCredentialError,
    verify_task_credential,
)
from xiosync.services.workflows import (
    CompletionOutcome,
    DeadLetterNotFoundError,
    DeadLetterRecord,
    InactiveLeaseError,
    NonCompletableError,
    TaskNotFoundError,
    TaskRecord,
    UnleaseableError,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ORG_ID = new_id()
_ACTOR_ID = new_id()
_TASK_ID = new_id()
_LEASE_ID = new_id()
_WORKER_ID = new_id()
_CAPABILITY_ID = new_id()
_RUN_ID = new_id()
_DL_ID = new_id()
_PROPOSAL_ID = new_id()

# The lease endpoint mints a task credential with the real wall clock as ``iat``
# and the lease expiry as ``exp``; the credential TTL is capped at one hour. So
# the mocked lease expiry must be a near-future, wall-clock-relative instant.
# Microseconds are stripped so the JWT's integer-second ``exp`` round-trips
# exactly through verify (INV-TASK-SEC-2 lifetime check).
_NOW = datetime.now(UTC).replace(microsecond=0)
_EXPIRES = _NOW + timedelta(minutes=5)

_CONTEXT = OrgContext(
    auth_identity_id=new_id(),
    actor_id=_ACTOR_ID,
    organization_id=_ORG_ID,
    session_id=new_id(),
    platform_role=PlatformRole.NONE,
    membership_role=MembershipRole.ORG_MEMBER,
)

_LEASED_TASK = TaskRecord(
    id=_TASK_ID,
    organization_id=_ORG_ID,
    run_id=_RUN_ID,
    node_id="fetch",
    capability_id=_CAPABILITY_ID,
    state="leased",
    attempts=1,
    lease_id=_LEASE_ID,
    leased_by=_WORKER_ID,
    lease_expires_at=_EXPIRES,
    input={"url": "https://example.com"},
    progress=None,
    progress_updated_at=None,
    priority=5,
    checkpoint=None,
    checkpoint_at=None,
)

_COMPLETED_TASK = TaskRecord(
    id=_TASK_ID,
    organization_id=_ORG_ID,
    run_id=_RUN_ID,
    node_id="fetch",
    capability_id=_CAPABILITY_ID,
    state="completed",
    attempts=1,
    lease_id=None,
    leased_by=None,
    lease_expires_at=None,
    input=None,
    progress=None,
    progress_updated_at=None,
    priority=5,
    checkpoint=None,
    checkpoint_at=None,
)

_DEAD_LETTER = DeadLetterRecord(
    id=_DL_ID,
    organization_id=_ORG_ID,
    task_id=_TASK_ID,
    state="open",
    failure_reason="capability timed out",
    proposal_id=None,
    diagnosis=None,
    stack_trace=None,
    last_checkpoint=None,
    original_input=None,
    dl_metadata={},
    attempts=1,
)


def _make_app(mock_service: MagicMock) -> FastAPI:
    """Build a minimal FastAPI app with the routers under test and injected deps."""
    app = FastAPI()
    app.include_router(execution_router, prefix="/api/v1")
    app.include_router(dlq_router, prefix="/api/v1")

    mock_session = MagicMock()

    # Inject org_context and org_session via a lightweight ASGI middleware so
    # every handler can read them from request.state without requiring the full
    # AuthenticationMiddleware + real DB + JWT stack.
    @app.middleware("http")
    async def inject_state(request: Request, call_next: Any) -> Any:
        request.state.org_context = _CONTEXT
        request.state.org_session = mock_session
        request.state.request_id = "test-request-id"
        return await call_next(request)

    # Patch WorkflowService so the routers call our mock instead.
    with patch("xiosync.api.routers.execution.WorkflowService", return_value=mock_service):
        with patch("xiosync.api.routers.dlq.WorkflowService", return_value=mock_service):
            # TestClient starts the app; patches must be active during the
            # client's lifecycle, so we return the app and let the test manage
            # the patch context using monkeypatch.setattr instead.
            pass

    return app


_TASK_CREDENTIAL_KEY = "unit-test-worker-credential-key-0123456789abcdef"


@pytest.fixture()
def execution_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, MagicMock]:
    """TestClient with a MagicMock WorkflowService wired into both routers."""
    mock_service = MagicMock()
    monkeypatch.setattr(
        "xiosync.api.routers.execution.WorkflowService", lambda _session: mock_service
    )
    monkeypatch.setattr(
        "xiosync.api.routers.dlq.WorkflowService", lambda _session: mock_service
    )
    # INV-TASK-SEC-1/2: the lease endpoint mints a task credential and needs the
    # worker-credential signing key (distinct from the user JWT secret — H7).
    monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _TASK_CREDENTIAL_KEY)
    app = _make_app(mock_service)
    return TestClient(app, raise_server_exceptions=True), mock_service


# ---------------------------------------------------------------------------
# Execution — lease_task (INV-EXEC-1)
# ---------------------------------------------------------------------------


def test_lease_task_success(execution_client: tuple[TestClient, MagicMock]) -> None:
    """POST /execution/tasks/{id}/lease with a queued task returns 200 + lease fields."""
    http, svc = execution_client
    svc.lease_task.return_value = _LEASED_TASK

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/lease",
        json={"leased_by": str(_WORKER_ID)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == str(_TASK_ID)
    assert body["lease_id"] == str(_LEASE_ID)
    assert body["leased_by"] == str(_WORKER_ID)
    assert body["state"] == "leased"
    assert body["attempts"] == 1

    # INV-TASK-SEC-1/2: a scoped, single-use task credential is minted at lease
    # time, bound to (task_id, worker_id) and expiring with the lease.
    assert body["task_credential"]
    assert body["task_credential_expires_at"] == _EXPIRES.isoformat()
    assert body["scoped_capabilities"] == [str(_CAPABILITY_ID)]

    claims = verify_task_credential(
        _TASK_CREDENTIAL_KEY,
        body["task_credential"],
        now=_NOW,
        expected_task_id=_TASK_ID,
        expected_worker_id=_WORKER_ID,
    )
    assert claims.task_id == _TASK_ID
    assert claims.worker_id == _WORKER_ID
    assert claims.lease_id == _LEASE_ID
    assert claims.organization_id == _ORG_ID
    assert claims.scoped_capabilities == (_CAPABILITY_ID,)
    assert claims.expires_at == _EXPIRES


def test_lease_credential_cannot_be_replayed_on_another_task(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-TASK-SEC-2: the minted credential is bound to (task_id, worker_id).

    Presenting it for a different task (or worker) is rejected — a leaked
    credential cannot be replayed elsewhere.
    """
    http, svc = execution_client
    svc.lease_task.return_value = _LEASED_TASK

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/lease",
        json={"leased_by": str(_WORKER_ID)},
    )
    token = resp.json()["task_credential"]

    with pytest.raises(TaskCredentialError):
        verify_task_credential(
            _TASK_CREDENTIAL_KEY,
            token,
            now=_NOW,
            expected_task_id=new_id(),  # a different task
            expected_worker_id=_WORKER_ID,
        )
    with pytest.raises(TaskCredentialError):
        verify_task_credential(
            _TASK_CREDENTIAL_KEY,
            token,
            now=_NOW,
            expected_task_id=_TASK_ID,
            expected_worker_id=new_id(),  # a different worker
        )


def test_lease_task_not_found_returns_404(execution_client: tuple[TestClient, MagicMock]) -> None:
    """Leasing a missing task returns 404 problem+json."""
    http, svc = execution_client
    svc.lease_task.side_effect = TaskNotFoundError(_TASK_ID)

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/lease",
        json={"leased_by": str(_WORKER_ID)},
    )

    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == "task_not_found"


def test_lease_task_already_leased_returns_409(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-EXEC-1: leasing a non-queued task returns 409."""
    http, svc = execution_client
    svc.lease_task.side_effect = UnleaseableError(_TASK_ID, "leased")

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/lease",
        json={"leased_by": str(_WORKER_ID)},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "task_not_leaseable"


def test_lease_task_with_explicit_duration(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """duration_seconds is forwarded to the service as a timedelta."""
    http, svc = execution_client
    svc.lease_task.return_value = _LEASED_TASK

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/lease",
        json={"leased_by": str(_WORKER_ID), "duration_seconds": 120},
    )

    assert resp.status_code == 200
    call_kwargs = svc.lease_task.call_args
    assert call_kwargs.kwargs["duration"] == timedelta(seconds=120)


# ---------------------------------------------------------------------------
# Execution — heartbeat_task
# ---------------------------------------------------------------------------


def test_heartbeat_task_success(execution_client: tuple[TestClient, MagicMock]) -> None:
    """POST /execution/tasks/{id}/heartbeat with correct lease_id returns 200."""
    http, svc = execution_client
    svc.heartbeat_task.return_value = _LEASED_TASK

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/heartbeat",
        json={"lease_id": str(_LEASE_ID)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["lease_id"] == str(_LEASE_ID)
    assert "lease_expires_at" in body


def test_heartbeat_task_inactive_lease_returns_409(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """A mismatched or expired lease_id raises InactiveLeaseError → 409."""
    http, svc = execution_client
    svc.heartbeat_task.side_effect = InactiveLeaseError(_TASK_ID, "lease_id mismatch")

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/heartbeat",
        json={"lease_id": str(uuid.uuid4())},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "lease_inactive"


# ---------------------------------------------------------------------------
# Execution — complete_task (INV-EXEC-2)
# ---------------------------------------------------------------------------


def test_complete_task_success(execution_client: tuple[TestClient, MagicMock]) -> None:
    """INV-EXEC-2: normal completion returns duplicate=false."""
    http, svc = execution_client
    svc.complete_task.return_value = CompletionOutcome(
        task_id=_TASK_ID, state="completed", result=None, duplicate=False
    )

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/complete",
        json={"lease_id": str(_LEASE_ID)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "completed"
    assert body["duplicate"] is False


def test_complete_task_idempotent(execution_client: tuple[TestClient, MagicMock]) -> None:
    """INV-EXEC-2: duplicate completion returns duplicate=true, not an error."""
    http, svc = execution_client
    svc.complete_task.return_value = CompletionOutcome(
        task_id=_TASK_ID, state="completed", result=None, duplicate=True
    )

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/complete",
        json={"lease_id": str(_LEASE_ID)},
    )

    assert resp.status_code == 200
    assert resp.json()["duplicate"] is True


def test_complete_task_not_completable_returns_409(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """Completing a non-leased task returns 409."""
    http, svc = execution_client
    svc.complete_task.side_effect = NonCompletableError(_TASK_ID, "queued")

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/complete",
        json={"lease_id": str(_LEASE_ID)},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "task_not_completable"


def test_complete_task_expired_lease_returns_409(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """An expired lease_id on completion returns 409."""
    http, svc = execution_client
    svc.complete_task.side_effect = InactiveLeaseError(_TASK_ID, "lease expired before completion")

    resp = http.post(
        f"/api/v1/execution/tasks/{_TASK_ID}/complete",
        json={"lease_id": str(_LEASE_ID)},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "lease_inactive"


# ---------------------------------------------------------------------------
# DLQ — get_dead_letter
# ---------------------------------------------------------------------------


def test_get_dead_letter_found(execution_client: tuple[TestClient, MagicMock]) -> None:
    """GET /dlq/{id} returns 200 with the record's fields."""
    http, svc = execution_client
    svc.get_dead_letter.return_value = _DEAD_LETTER

    resp = http.get(f"/api/v1/dlq/{_DL_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(_DL_ID)
    assert body["state"] == "open"
    assert body["failure_reason"] == "capability timed out"


def test_get_dead_letter_not_found_returns_404(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """A missing dead-letter id returns 404."""
    http, svc = execution_client
    svc.get_dead_letter.return_value = None

    resp = http.get(f"/api/v1/dlq/{_DL_ID}")

    assert resp.status_code == 404
    assert resp.json()["code"] == "dead_letter_not_found"


# ---------------------------------------------------------------------------
# DLQ — propose_dlq_correction (INV-DLQ-2)
# ---------------------------------------------------------------------------


def test_propose_dlq_correction_success(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-DLQ-2: proposal attaches diagnosis and returns proposal_id + investigating."""
    http, svc = execution_client
    svc.propose_dlq_correction.return_value = _PROPOSAL_ID

    resp = http.post(
        f"/api/v1/dlq/{_DL_ID}/propose",
        json={"diagnosis": {"cause": "network timeout", "suggested_fix": "increase timeout"}},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["proposal_id"] == str(_PROPOSAL_ID)
    assert body["state"] == "investigating"


def test_propose_dlq_already_investigating_returns_409(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-DLQ-2: proposing on a non-open record returns 409."""
    http, svc = execution_client
    svc.propose_dlq_correction.side_effect = ValueError(
        "dead_letter is in state 'investigating' and does not accept a new proposal"
    )

    resp = http.post(
        f"/api/v1/dlq/{_DL_ID}/propose",
        json={"diagnosis": {"cause": "test"}},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "proposal_not_accepted"


def test_propose_dlq_not_found_returns_404(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """Proposing on a missing dead-letter id returns 404."""
    http, svc = execution_client
    svc.propose_dlq_correction.side_effect = DeadLetterNotFoundError(_DL_ID)

    resp = http.post(
        f"/api/v1/dlq/{_DL_ID}/propose",
        json={"diagnosis": {"cause": "test"}},
    )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DLQ — resolve_dead_letter (INV-DLQ-3)
# ---------------------------------------------------------------------------


def test_resolve_dead_letter_success(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-DLQ-3: resolve with explicit_approval=true succeeds."""
    http, svc = execution_client
    svc.resolve_dead_letter.return_value = None  # no return value

    resp = http.post(
        f"/api/v1/dlq/{_DL_ID}/resolve",
        json={"explicit_approval": True},
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "resolved"
    svc.resolve_dead_letter.assert_called_once()
    call_kwargs = svc.resolve_dead_letter.call_args
    assert call_kwargs.kwargs["explicit_approval"] is True


def test_resolve_dead_letter_auto_approval_rejected(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-DLQ-3: explicit_approval=false is a validation error (422 from Pydantic)."""
    http, svc = execution_client

    resp = http.post(
        f"/api/v1/dlq/{_DL_ID}/resolve",
        json={"explicit_approval": False},
    )

    # Pydantic model_validator rejects this before the handler is reached.
    assert resp.status_code == 422
    # The service must never have been called.
    svc.resolve_dead_letter.assert_not_called()


def test_resolve_dead_letter_not_investigating_returns_409(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-DLQ-3: resolving a record not in 'investigating' state returns 409."""
    http, svc = execution_client
    svc.resolve_dead_letter.side_effect = ValueError(
        "dead_letter cannot be resolved: state='open'"
    )

    resp = http.post(
        f"/api/v1/dlq/{_DL_ID}/resolve",
        json={"explicit_approval": True},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "resolve_not_permitted"


def test_resolve_dead_letter_not_found_returns_404(
    execution_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-DLQ-3: resolving a missing dead-letter id returns 404."""
    http, svc = execution_client
    svc.resolve_dead_letter.side_effect = DeadLetterNotFoundError(_DL_ID)

    resp = http.post(
        f"/api/v1/dlq/{_DL_ID}/resolve",
        json={"explicit_approval": True},
    )

    assert resp.status_code == 404
