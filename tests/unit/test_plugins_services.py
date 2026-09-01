"""Unit tests for xiosync/services/plugins.py.

Two concerns, both database-free:

* **The strict execution boundary** (:func:`sanitize_environment`,
  :func:`run_in_sandbox`) is exercised with a *real* subprocess running harmless
  ``python -c`` snippets, proving the environment is stripped, the filesystem
  jail is enforced and destroyed, and the wall-clock quota kills a slow plugin.
* **The approval gate** (:meth:`PluginService.install_plugin` /
  ``approve_installation`` / ``activate_installation`` / ``execute_plugin_rpc``)
  is exercised with a hand-rolled fake ``Session`` (the service reads via
  ``scalar``/``scalars`` and writes via ``add``/``flush``, all trivially fakeable)
  so we can assert that nothing reaches an operational state — or spawns a
  process — without explicit approval.

Tests are named after the invariant (INV-PLUGIN-1..3) they protect.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.domain.plugins import (
    INSTALLATION_STATE_ACTIVE,
    INSTALLATION_STATE_APPROVED,
    INSTALLATION_STATE_PENDING_APPROVAL,
    INSTALLATION_STATE_REVOKED,
    INSTALLATION_STATE_SUSPENDED,
)
from xiosync.services import plugins as plugins_module
from xiosync.services.plugins import (
    InstallationNotApprovableError,
    PluginAlreadyInstalledError,
    PluginExecutionError,
    PluginNotFoundError,
    PluginNotOperationalError,
    PluginService,
    PluginTimeoutError,
    SandboxResult,
    UnknownRpcMethodError,
    env_key_is_sensitive,
    run_in_sandbox,
    sanitize_environment,
)

_ORG_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_APPROVER_ID = uuid.uuid4()
_PLUGIN_ID = uuid.uuid4()
_NOW = datetime(2026, 7, 24, 12, tzinfo=UTC)


def _context() -> OrgContext:
    return OrgContext(
        auth_identity_id=uuid.uuid4(),
        actor_id=_ACTOR_ID,
        organization_id=_ORG_ID,
        session_id=uuid.uuid4(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )


# ===========================================================================
# Environment sanitization (INV-PLUGIN-1: no ambient secrets)
# ===========================================================================


def test_sanitize_environment_is_allowlist_only() -> None:
    """Only allowlisted keys survive; everything else is dropped."""
    base = {
        "PATH": "/usr/bin",
        "LANG": "en_US.UTF-8",
        "SOME_RANDOM_APP_VAR": "keep-me?",  # not allowlisted -> dropped
    }
    clean = sanitize_environment(base)
    assert clean["PATH"] == "/usr/bin"
    assert clean["LANG"] == "en_US.UTF-8"
    assert "SOME_RANDOM_APP_VAR" not in clean


@pytest.mark.parametrize(
    "secret_key",
    [
        "DATABASE_URL",
        "XIOSYNC_AUTH_SECRET",
        "WORKER_CREDENTIAL_KEY",
        "JWT_SECRET",
        "AWS_SECRET_ACCESS_KEY",
        "SESSION_COOKIE",
        "MY_API_TOKEN",
        "DB_PASSWORD",
    ],
)
def test_sanitize_environment_strips_every_secret(secret_key: str) -> None:
    """INV-PLUGIN-1: no platform secret / credential reaches the plugin."""
    base = {"PATH": "/usr/bin", secret_key: "super-secret-value"}
    clean = sanitize_environment(base)
    assert secret_key not in clean
    assert "super-secret-value" not in clean.values()


def test_sanitize_environment_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIOSYNC_AUTH_SECRET", "leaky")
    monkeypatch.setenv("PATH", "/sbin")
    clean = sanitize_environment()
    assert "XIOSYNC_AUTH_SECRET" not in clean
    assert clean["PATH"] == "/sbin"


def test_sanitize_environment_rejects_sensitive_extra_env() -> None:
    """A caller cannot smuggle a secret back in through extra_env."""
    with pytest.raises(ValueError, match="sensitive"):
        sanitize_environment({"PATH": "/usr/bin"}, extra_env={"API_TOKEN": "x"})


def test_sanitize_environment_allows_benign_extra_env() -> None:
    clean = sanitize_environment({"PATH": "/usr/bin"}, extra_env={"PLUGIN_MODE": "fast"})
    assert clean["PLUGIN_MODE"] == "fast"


@pytest.mark.parametrize(
    "key,expected",
    [
        ("DATABASE_URL", True),
        ("XIOSYNC_ENVIRONMENT", True),
        ("aws_access_key_id", True),
        ("refresh_token", True),
        ("PATH", False),
        ("LANG", False),
        ("PLUGIN_MODE", False),
    ],
)
def test_env_key_is_sensitive(key: str, expected: bool) -> None:
    assert env_key_is_sensitive(key) is expected


# ===========================================================================
# Sandbox boundary (INV-PLUGIN-1) — real subprocess, harmless commands
# ===========================================================================


def test_run_in_sandbox_child_environment_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launched process cannot see a secret present in the host env."""
    monkeypatch.setenv("XIOSYNC_AUTH_SECRET", "top-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    # Child prints its own environment as JSON.
    argv = [sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"]
    result = run_in_sandbox(argv, timeout_seconds=10)
    assert result.returncode == 0
    child_env = _loads(result.stdout)
    assert "XIOSYNC_AUTH_SECRET" not in child_env
    assert "DATABASE_URL" not in child_env


def test_run_in_sandbox_cwd_is_a_temporary_jail_that_is_removed() -> None:
    """The child runs inside a fresh temp jail that no longer exists afterwards."""
    argv = [sys.executable, "-c", "import os; print(os.getcwd())"]
    result = run_in_sandbox(argv, timeout_seconds=10)
    jail_path = result.stdout.strip()
    assert os.path.basename(jail_path).startswith("xiosync-plugin-")
    # The jail is destroyed once the invocation returns.
    assert not os.path.exists(jail_path)


def test_run_in_sandbox_home_and_tmpdir_point_at_jail() -> None:
    argv = [
        sys.executable,
        "-c",
        "import os; print(os.environ['HOME']); print(os.environ['TMPDIR'])",
    ]
    result = run_in_sandbox(argv, timeout_seconds=10)
    home, tmpdir = result.stdout.strip().splitlines()
    assert home == tmpdir
    assert os.path.basename(home).startswith("xiosync-plugin-")


def test_run_in_sandbox_forwards_stdin_payload() -> None:
    argv = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
    result = run_in_sandbox(argv, timeout_seconds=10, stdin_payload="hello")
    assert result.stdout == "HELLO"


def test_run_in_sandbox_times_out_slow_plugin() -> None:
    """INV-PLUGIN-1: a plugin that overruns its wall-clock quota is killed."""
    argv = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(PluginTimeoutError):
        run_in_sandbox(argv, timeout_seconds=1)


def test_run_in_sandbox_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        run_in_sandbox([sys.executable, "-c", "pass"], timeout_seconds=0)


def test_run_in_sandbox_rejects_empty_argv() -> None:
    with pytest.raises(ValueError, match="argv"):
        run_in_sandbox([], timeout_seconds=5)


def test_run_in_sandbox_surfaces_nonzero_exit() -> None:
    argv = [sys.executable, "-c", "import sys; sys.exit(3)"]
    result = run_in_sandbox(argv, timeout_seconds=10)
    assert result.returncode == 3


# ===========================================================================
# Fake session for the control-plane approval-gate tests
# ===========================================================================


class _FakeScalarList:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return list(self._values)


class FakeSession:
    """A minimal stand-in for the parts of ``Session`` PluginService uses.

    ``scalar`` returns queued values in order; ``scalars`` returns a queued list
    wrapper; ``add``/``flush`` record calls. This lets us drive the service's
    branch logic without a database (mirrors the repo's mock-based unit style).
    """

    def __init__(
        self,
        *,
        scalar_results: list[Any] | None = None,
        scalars_results: list[list[Any]] | None = None,
    ) -> None:
        self._scalar_results = list(scalar_results or [])
        self._scalars_results = list(scalars_results or [])
        self.added: list[Any] = []
        self.flush_count = 0

    def scalar(self, _statement: Any) -> Any:
        if not self._scalar_results:
            raise AssertionError("unexpected scalar() call")
        return self._scalar_results.pop(0)

    def scalars(self, _statement: Any) -> _FakeScalarList:
        if not self._scalars_results:
            raise AssertionError("unexpected scalars() call")
        return _FakeScalarList(self._scalars_results.pop(0))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flush_count += 1


def _plugin_row(*, timeout_seconds: int = 30, entrypoint: str = "ghcr.io/acme/x@sha256:abc") -> Any:
    return SimpleNamespace(
        id=_PLUGIN_ID,
        organization_id=_ORG_ID,
        required_capability_id=uuid.uuid4(),
        entrypoint=entrypoint,
        timeout_seconds=timeout_seconds,
    )


def _installation_row(*, state: str) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        plugin_id=_PLUGIN_ID,
        state=state,
        requested_by=_ACTOR_ID,
        approved_by=None,
        approved_at=None,
        grant_id=None,
        updated_at=None,
    )


# ===========================================================================
# install_plugin — INV-PLUGIN-3: installation is approval-gated
# ===========================================================================


def test_install_plugin_lands_pending_approval_never_active() -> None:
    """INV-PLUGIN-3: a fresh install can only land in pending_approval."""
    session = FakeSession(scalar_results=[_plugin_row(), None])  # plugin exists, no prior install
    service = PluginService(session)

    record = service.install_plugin(_context(), _PLUGIN_ID, requested_by=_ACTOR_ID)

    assert record.state == INSTALLATION_STATE_PENDING_APPROVAL
    assert record.approved_by is None
    assert record.grant_id is None
    # The row that was persisted is also pending — no auto-approval side effect.
    assert session.added[0].state == INSTALLATION_STATE_PENDING_APPROVAL
    assert session.flush_count == 1


def test_install_plugin_has_no_approval_shortcut_parameter() -> None:
    """The install API surface cannot express 'install + approve' in one call."""
    import inspect

    params = set(inspect.signature(PluginService.install_plugin).parameters)
    # No parameter can flip the landing state to approved/active.
    assert params == {"self", "ctx", "plugin_id", "requested_by"}


def test_install_plugin_unknown_plugin_raises() -> None:
    session = FakeSession(scalar_results=[None])
    service = PluginService(session)
    with pytest.raises(PluginNotFoundError):
        service.install_plugin(_context(), _PLUGIN_ID, requested_by=_ACTOR_ID)
    assert session.added == []


def test_install_plugin_duplicate_raises() -> None:
    session = FakeSession(
        scalar_results=[_plugin_row(), _installation_row(state=INSTALLATION_STATE_PENDING_APPROVAL)]
    )
    service = PluginService(session)
    with pytest.raises(PluginAlreadyInstalledError):
        service.install_plugin(_context(), _PLUGIN_ID, requested_by=_ACTOR_ID)
    assert session.added == []


# ===========================================================================
# approve_installation — the explicit, authorized gate that mints the grant
# ===========================================================================


def test_approve_installation_from_pending_mints_grant_and_advances() -> None:
    pending = _installation_row(state=INSTALLATION_STATE_PENDING_APPROVAL)
    plugin = _plugin_row()
    session = FakeSession(scalar_results=[pending, plugin])
    service = PluginService(session)

    record = service.approve_installation(
        _context(), pending.id, approved_by=_APPROVER_ID, now=_NOW
    )

    assert record.state == INSTALLATION_STATE_APPROVED
    assert record.approved_by == _APPROVER_ID
    assert record.grant_id is not None
    # A capability Grant was minted for the plugin's required capability.
    grant = session.added[0]
    assert grant.capability_id == plugin.required_capability_id
    assert grant.state == "active"
    assert grant.actor_id == pending.requested_by


@pytest.mark.parametrize(
    "state",
    [
        INSTALLATION_STATE_APPROVED,
        INSTALLATION_STATE_ACTIVE,
        INSTALLATION_STATE_SUSPENDED,
        INSTALLATION_STATE_REVOKED,
    ],
)
def test_approve_installation_rejects_non_pending(state: str) -> None:
    """INV-PLUGIN-3: only a pending install may be approved; no re-approval."""
    row = _installation_row(state=state)
    session = FakeSession(scalar_results=[row])
    service = PluginService(session)
    with pytest.raises(InstallationNotApprovableError):
        service.approve_installation(_context(), row.id, approved_by=_APPROVER_ID)
    # No grant minted for a non-pending install.
    assert session.added == []


# ===========================================================================
# activate_installation — approved -> active only
# ===========================================================================


def test_activate_installation_from_approved() -> None:
    row = _installation_row(state=INSTALLATION_STATE_APPROVED)
    session = FakeSession(scalar_results=[row])
    service = PluginService(session)
    record = service.activate_installation(_context(), row.id, now=_NOW)
    assert record.state == INSTALLATION_STATE_ACTIVE


def test_activate_installation_cannot_skip_approval() -> None:
    """INV-PLUGIN-3: a pending install can never jump straight to active."""
    row = _installation_row(state=INSTALLATION_STATE_PENDING_APPROVAL)
    session = FakeSession(scalar_results=[row])
    service = PluginService(session)
    with pytest.raises(InstallationNotApprovableError):
        service.activate_installation(_context(), row.id)


# ===========================================================================
# execute_plugin_rpc — INV-PLUGIN-3 (only active runs) + INV-PLUGIN-2 (typed)
# ===========================================================================


@pytest.mark.parametrize(
    "state",
    [
        INSTALLATION_STATE_PENDING_APPROVAL,
        INSTALLATION_STATE_APPROVED,
        INSTALLATION_STATE_SUSPENDED,
        INSTALLATION_STATE_REVOKED,
    ],
)
def test_execute_refuses_non_active_install_and_never_spawns(
    state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-PLUGIN-3: a non-active install must never launch a process."""
    session = FakeSession(scalar_results=[_installation_row(state=state)])
    service = PluginService(session)

    spawned = False

    def _boom(*_args: Any, **_kwargs: Any) -> SandboxResult:
        nonlocal spawned
        spawned = True
        raise AssertionError("run_in_sandbox must not be called for a non-active install")

    monkeypatch.setattr(plugins_module, "run_in_sandbox", _boom)

    with pytest.raises(PluginNotOperationalError):
        service.execute_plugin_rpc(
            _context(), uuid.uuid4(), method="normalize", params={}
        )
    assert spawned is False


def test_execute_active_install_dispatches_declared_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _installation_row(state=INSTALLATION_STATE_ACTIVE)
    session = FakeSession(
        scalar_results=[active, _plugin_row()],
        scalars_results=[["normalize"]],
    )
    service = PluginService(session)

    captured: dict[str, Any] = {}

    def _fake_sandbox(argv: Any, **kwargs: Any) -> SandboxResult:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SandboxResult(returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(plugins_module, "run_in_sandbox", _fake_sandbox)

    result = service.execute_plugin_rpc(
        _context(), active.id, method="normalize", params={"n": 1}
    )

    assert result.output == {"ok": True}
    # The declared timeout flows into the sandbox call.
    assert captured["kwargs"]["timeout_seconds"] == 30
    assert "normalize" in captured["kwargs"]["stdin_payload"]


def test_execute_rejects_undeclared_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-PLUGIN-2: only a manifest-declared method may be dispatched."""
    active = _installation_row(state=INSTALLATION_STATE_ACTIVE)
    session = FakeSession(
        scalar_results=[active, _plugin_row()],
        scalars_results=[["normalize"]],
    )
    service = PluginService(session)
    monkeypatch.setattr(
        plugins_module,
        "run_in_sandbox",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    with pytest.raises(UnknownRpcMethodError):
        service.execute_plugin_rpc(_context(), active.id, method="danger", params={})


def test_execute_nonzero_exit_raises_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _installation_row(state=INSTALLATION_STATE_ACTIVE)
    session = FakeSession(
        scalar_results=[active, _plugin_row()],
        scalars_results=[["normalize"]],
    )
    service = PluginService(session)
    monkeypatch.setattr(
        plugins_module,
        "run_in_sandbox",
        lambda *a, **k: SandboxResult(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(PluginExecutionError, match="exited with code 1"):
        service.execute_plugin_rpc(_context(), active.id, method="normalize", params={})


def test_execute_non_json_reply_raises_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _installation_row(state=INSTALLATION_STATE_ACTIVE)
    session = FakeSession(
        scalar_results=[active, _plugin_row()],
        scalars_results=[["normalize"]],
    )
    service = PluginService(session)
    monkeypatch.setattr(
        plugins_module,
        "run_in_sandbox",
        lambda *a, **k: SandboxResult(returncode=0, stdout="not json", stderr=""),
    )
    with pytest.raises(PluginExecutionError, match="non-JSON"):
        service.execute_plugin_rpc(_context(), active.id, method="normalize", params={})


def _loads(text: str) -> dict[str, Any]:
    import json

    return json.loads(text)
