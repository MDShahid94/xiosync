"""Unit tests for the sandboxed-plugin API router.

Tests the HTTP contracts of:
- ``POST /api/v1/plugins/{plugin_id}/install``                      (INV-PLUGIN-3)
- ``POST /api/v1/plugins/installations/{installation_id}/approve``  (INV-PLUGIN-3/1)
- ``POST /api/v1/plugins/installations/{installation_id}/activate`` (INV-PLUGIN-3)
- ``GET  /api/v1/plugins/installations/{installation_id}``
- ``POST /api/v1/plugins/installations/{installation_id}/rpc``      (INV-PLUGIN-1/2/3)

These are router-layer unit tests — no real database, no real migrations. The
``PluginService`` is replaced by a ``MagicMock`` (INV-ROADMAP-3: using
``unittest.mock.MagicMock`` in test files is explicitly authorised). The
``AuthenticationMiddleware`` is bypassed by injecting ``org_context`` +
``org_session`` onto ``request.state`` via a thin pass-through middleware.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from xiosync.api.routers.plugins import router as plugins_router
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.platform.ids import new_id
from xiosync.services.plugins import (
    InstallationNotApprovableError,
    InstallationNotFoundError,
    PluginAlreadyInstalledError,
    PluginExecutionError,
    PluginInstallationRecord,
    PluginNotFoundError,
    PluginNotOperationalError,
    PluginRpcResult,
    PluginTimeoutError,
    UnknownRpcMethodError,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ORG_ID = new_id()
_ACTOR_ID = new_id()
_PLUGIN_ID = new_id()
_INSTALLATION_ID = new_id()
_GRANT_ID = new_id()
_REQUESTED_BY = new_id()
_APPROVED_BY = new_id()

_CONTEXT = OrgContext(
    auth_identity_id=new_id(),
    actor_id=_ACTOR_ID,
    organization_id=_ORG_ID,
    session_id=new_id(),
    platform_role=PlatformRole.NONE,
    membership_role=MembershipRole.ORG_ADMIN,
)

_PENDING_INSTALL = PluginInstallationRecord(
    id=_INSTALLATION_ID,
    organization_id=_ORG_ID,
    plugin_id=_PLUGIN_ID,
    state="pending_approval",
    requested_by=_REQUESTED_BY,
    approved_by=None,
    grant_id=None,
)

_APPROVED_INSTALL = PluginInstallationRecord(
    id=_INSTALLATION_ID,
    organization_id=_ORG_ID,
    plugin_id=_PLUGIN_ID,
    state="approved",
    requested_by=_REQUESTED_BY,
    approved_by=_APPROVED_BY,
    grant_id=_GRANT_ID,
)

_ACTIVE_INSTALL = PluginInstallationRecord(
    id=_INSTALLATION_ID,
    organization_id=_ORG_ID,
    plugin_id=_PLUGIN_ID,
    state="active",
    requested_by=_REQUESTED_BY,
    approved_by=_APPROVED_BY,
    grant_id=_GRANT_ID,
)


def _make_app(mock_service: MagicMock) -> FastAPI:
    """Build a minimal FastAPI app with the plugins router and injected deps."""
    app = FastAPI()
    app.include_router(plugins_router, prefix="/api/v1")

    mock_session = MagicMock()

    @app.middleware("http")
    async def inject_state(request: Request, call_next: Any) -> Any:
        request.state.org_context = _CONTEXT
        request.state.org_session = mock_session
        request.state.request_id = "test-request-id"
        return await call_next(request)

    return app


@pytest.fixture()
def plugins_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, MagicMock]:
    """TestClient with a MagicMock PluginService wired into the router."""
    mock_service = MagicMock()
    monkeypatch.setattr(
        "xiosync.api.routers.plugins.PluginService", lambda _session: mock_service
    )
    app = _make_app(mock_service)
    return TestClient(app, raise_server_exceptions=True), mock_service


# ---------------------------------------------------------------------------
# install_plugin (INV-PLUGIN-3)
# ---------------------------------------------------------------------------


def test_install_plugin_success_lands_pending_approval(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-3: install returns 201 and a record in pending_approval."""
    http, svc = plugins_client
    svc.install_plugin.return_value = _PENDING_INSTALL

    resp = http.post(
        f"/api/v1/plugins/{_PLUGIN_ID}/install",
        json={"requested_by": str(_REQUESTED_BY)},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(_INSTALLATION_ID)
    assert body["plugin_id"] == str(_PLUGIN_ID)
    # INV-PLUGIN-3: the created record can only land in pending_approval.
    assert body["state"] == "pending_approval"
    assert body["approved_by"] is None
    assert body["grant_id"] is None

    # The requested_by from the body is forwarded to the service.
    call = svc.install_plugin.call_args
    assert call.args[0] is _CONTEXT
    assert call.args[1] == _PLUGIN_ID
    assert call.kwargs["requested_by"] == _REQUESTED_BY


def test_install_plugin_cannot_shortcut_to_active(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-3: there is no request field that approves/activates in one step.

    An ``active``/``approved`` value in the request body is rejected by the
    strict (``extra="forbid"``) request model, so the install endpoint cannot be
    coerced past the approval gate.
    """
    http, svc = plugins_client
    svc.install_plugin.return_value = _PENDING_INSTALL

    resp = http.post(
        f"/api/v1/plugins/{_PLUGIN_ID}/install",
        json={"requested_by": str(_REQUESTED_BY), "state": "active"},
    )

    assert resp.status_code == 422
    svc.install_plugin.assert_not_called()


def test_install_plugin_not_found_returns_404(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """Installing a missing plugin returns 404 problem+json."""
    http, svc = plugins_client
    svc.install_plugin.side_effect = PluginNotFoundError(_PLUGIN_ID)

    resp = http.post(
        f"/api/v1/plugins/{_PLUGIN_ID}/install",
        json={"requested_by": str(_REQUESTED_BY)},
    )

    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == "plugin_not_found"


def test_install_plugin_already_installed_returns_409(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """A plugin already installed in this org returns 409."""
    http, svc = plugins_client
    svc.install_plugin.side_effect = PluginAlreadyInstalledError(_PLUGIN_ID)

    resp = http.post(
        f"/api/v1/plugins/{_PLUGIN_ID}/install",
        json={"requested_by": str(_REQUESTED_BY)},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "plugin_already_installed"


def test_install_plugin_missing_body_returns_422(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """requested_by is required; omitting it is a validation error."""
    http, svc = plugins_client

    resp = http.post(f"/api/v1/plugins/{_PLUGIN_ID}/install", json={})

    assert resp.status_code == 422
    svc.install_plugin.assert_not_called()


# ---------------------------------------------------------------------------
# approve_installation (INV-PLUGIN-3/1)
# ---------------------------------------------------------------------------


def test_approve_installation_success(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-3: approve advances the install to approved and links a grant."""
    http, svc = plugins_client
    svc.approve_installation.return_value = _APPROVED_INSTALL

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/approve",
        json={"approved_by": str(_APPROVED_BY)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "approved"
    assert body["approved_by"] == str(_APPROVED_BY)
    assert body["grant_id"] == str(_GRANT_ID)

    call = svc.approve_installation.call_args
    assert call.args[1] == _INSTALLATION_ID
    assert call.kwargs["approved_by"] == _APPROVED_BY


def test_approve_installation_not_found_returns_404(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """Approving a missing install returns 404."""
    http, svc = plugins_client
    svc.approve_installation.side_effect = InstallationNotFoundError(_INSTALLATION_ID)

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/approve",
        json={"approved_by": str(_APPROVED_BY)},
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == "installation_not_found"


def test_approve_installation_wrong_state_returns_409(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-3: approving a non-pending install returns 409."""
    http, svc = plugins_client
    svc.approve_installation.side_effect = InstallationNotApprovableError(
        _INSTALLATION_ID, "active", "approve"
    )

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/approve",
        json={"approved_by": str(_APPROVED_BY)},
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "installation_not_approvable"
    assert "active" in body["detail"]


# ---------------------------------------------------------------------------
# activate_installation (INV-PLUGIN-3)
# ---------------------------------------------------------------------------


def test_activate_installation_success(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-3: activate advances approved → active."""
    http, svc = plugins_client
    svc.activate_installation.return_value = _ACTIVE_INSTALL

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/activate"
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "active"
    svc.activate_installation.assert_called_once()
    assert svc.activate_installation.call_args.args[1] == _INSTALLATION_ID


def test_activate_installation_not_found_returns_404(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """Activating a missing install returns 404."""
    http, svc = plugins_client
    svc.activate_installation.side_effect = InstallationNotFoundError(_INSTALLATION_ID)

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/activate"
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == "installation_not_found"


def test_activate_installation_wrong_state_returns_409(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-3: activating a non-approved install returns 409 (gate not bypassable)."""
    http, svc = plugins_client
    svc.activate_installation.side_effect = InstallationNotApprovableError(
        _INSTALLATION_ID, "pending_approval", "activate"
    )

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/activate"
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "installation_not_activatable"
    assert "pending_approval" in body["detail"]


# ---------------------------------------------------------------------------
# get_installation
# ---------------------------------------------------------------------------


def test_get_installation_found(plugins_client: tuple[TestClient, MagicMock]) -> None:
    """GET returns 200 with the installation record's fields."""
    http, svc = plugins_client
    svc.get_installation.return_value = _ACTIVE_INSTALL

    resp = http.get(f"/api/v1/plugins/installations/{_INSTALLATION_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(_INSTALLATION_ID)
    assert body["state"] == "active"


def test_get_installation_not_found_returns_404(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """A missing installation id returns 404."""
    http, svc = plugins_client
    svc.get_installation.return_value = None

    resp = http.get(f"/api/v1/plugins/installations/{_INSTALLATION_ID}")

    assert resp.status_code == 404
    assert resp.json()["code"] == "installation_not_found"


# ---------------------------------------------------------------------------
# execute_plugin_rpc (INV-PLUGIN-1/2/3)
# ---------------------------------------------------------------------------


def test_execute_rpc_success(plugins_client: tuple[TestClient, MagicMock]) -> None:
    """INV-PLUGIN-2: a declared method on an active install returns 200 + output."""
    http, svc = plugins_client
    svc.execute_plugin_rpc.return_value = PluginRpcResult(
        method="summarize", output={"summary": "ok", "tokens": 42}
    )

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"method": "summarize", "params": {"text": "hello"}},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["method"] == "summarize"
    assert body["output"] == {"summary": "ok", "tokens": 42}

    call = svc.execute_plugin_rpc.call_args
    assert call.args[1] == _INSTALLATION_ID
    assert call.kwargs["method"] == "summarize"
    assert call.kwargs["params"] == {"text": "hello"}


def test_execute_rpc_defaults_params_to_empty_object(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """params is optional and defaults to an empty JSON object (INV-PLUGIN-2)."""
    http, svc = plugins_client
    svc.execute_plugin_rpc.return_value = PluginRpcResult(method="ping", output="pong")

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"method": "ping"},
    )

    assert resp.status_code == 200
    assert resp.json()["output"] == "pong"
    assert svc.execute_plugin_rpc.call_args.kwargs["params"] == {}


def test_execute_rpc_non_object_params_returns_422(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-2: a non-object params payload is rejected before any spawn."""
    http, svc = plugins_client

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"method": "summarize", "params": ["not", "an", "object"]},
    )

    assert resp.status_code == 422
    svc.execute_plugin_rpc.assert_not_called()


def test_execute_rpc_missing_method_returns_422(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """method is required; omitting it is a validation error."""
    http, svc = plugins_client

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"params": {}},
    )

    assert resp.status_code == 422
    svc.execute_plugin_rpc.assert_not_called()


def test_execute_rpc_installation_not_found_returns_404(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """Executing against a missing install returns 404."""
    http, svc = plugins_client
    svc.execute_plugin_rpc.side_effect = InstallationNotFoundError(_INSTALLATION_ID)

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"method": "summarize"},
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == "installation_not_found"


def test_execute_rpc_not_operational_returns_409(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-3: executing a non-active install returns 409 (no process spawned)."""
    http, svc = plugins_client
    svc.execute_plugin_rpc.side_effect = PluginNotOperationalError(
        _INSTALLATION_ID, "pending_approval"
    )

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"method": "summarize"},
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "plugin_not_operational"
    assert "pending_approval" in body["detail"]


def test_execute_rpc_unknown_method_returns_404(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-2: an undeclared method is refused with 404."""
    http, svc = plugins_client
    svc.execute_plugin_rpc.side_effect = UnknownRpcMethodError("dangerous_undeclared")

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"method": "dangerous_undeclared"},
    )

    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "unknown_rpc_method"
    assert "dangerous_undeclared" in body["detail"]


def test_execute_rpc_timeout_returns_504(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """INV-PLUGIN-1: a wall-clock breach returns 504."""
    http, svc = plugins_client
    svc.execute_plugin_rpc.side_effect = PluginTimeoutError(30)

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"method": "slow"},
    )

    assert resp.status_code == 504
    assert resp.json()["code"] == "plugin_timeout"


def test_execute_rpc_execution_error_returns_502(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """A non-zero exit / unreadable reply returns 502."""
    http, svc = plugins_client
    svc.execute_plugin_rpc.side_effect = PluginExecutionError(
        "plugin produced a non-JSON RPC reply (INV-PLUGIN-2)"
    )

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"method": "broken"},
    )

    assert resp.status_code == 502
    assert resp.json()["code"] == "plugin_execution_failed"


def test_execute_rpc_value_error_returns_400(
    plugins_client: tuple[TestClient, MagicMock],
) -> None:
    """A service-raised ValueError surfaces as 400 invalid_rpc_params."""
    http, svc = plugins_client
    svc.execute_plugin_rpc.side_effect = ValueError(
        "params must be a JSON object (INV-PLUGIN-2)"
    )

    resp = http.post(
        f"/api/v1/plugins/installations/{_INSTALLATION_ID}/rpc",
        json={"method": "summarize"},
    )

    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_rpc_params"
