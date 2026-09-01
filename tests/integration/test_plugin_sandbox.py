"""Phase 5 Exit Gate — plugin sandbox integration tests (doc 07 §5, C10).

This suite rigorously exercises the *plugin execution boundary* end to end and
proves the three properties the exit gate requires:

1. **A plugin gets no ambient access (INV-PLUGIN-1).** A *real* dummy plugin is
   launched through the sandbox (both directly via :func:`run_in_sandbox` and
   through :meth:`PluginService.execute_plugin_rpc`) with the host environment
   deliberately polluted with a fake ``DATABASE_URL`` and platform secrets. The
   child reports its own environment and working directory back over stdio; the
   test asserts every credential was stripped, the DB URL is unreachable, and the
   process runs in a fresh, empty filesystem jail — so it cannot reach the
   database, platform secrets, or (lacking any credential *or* an allowlisted
   destination) a non-allowlisted host.

2. **An empty allowlist denies all network access (INV-PLUGIN-4).** The default
   is deny-all, never "allow everything": an empty ruleset permits nothing at the
   domain, service, and (with a real DB) persistence layers, and every allow-all
   host sentinel (``*``, ``0.0.0.0/0``, ``::/0``, …) is rejected at construction
   so no single rule can widen the policy to everything.

3. **Installing/activating/executing without approval is impossible
   (INV-PLUGIN-3).** Driven through the real FastAPI router with a
   ``TestClient``: the install endpoint can only ever mint a ``pending_approval``
   record, execution against any non-``active`` install is refused with 409
   (``plugin_not_operational``) *before a process is ever spawned*, activation of
   a still-pending install is refused, and execution only succeeds once the
   install is genuinely ``active``.

The always-on tests (subprocess + ASGI + pure predicates) need no database and
run everywhere. The ``@pytest.mark.integration`` tests additionally prove the
same guarantees against the real migrated PostgreSQL schema and skip cleanly
when ``DATABASE_URL`` is unset (doc 06 §10), following the repo convention.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from xiosync.api.app import create_app
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.domain.plugins import (
    INSTALLATION_STATE_ACTIVE,
    INSTALLATION_STATE_APPROVED,
    INSTALLATION_STATE_PENDING_APPROVAL,
    INSTALLATION_STATE_REVOKED,
    INSTALLATION_STATE_SUSPENDED,
    NetworkAllowRule,
    allowlist_default_denies,
    network_allows,
)
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.clock import FixedClock
from xiosync.platform.ids import new_id
from xiosync.services import plugins as plugins_module
from xiosync.services.plugins import (
    InstallationNotApprovableError,
    PluginNotOperationalError,
    PluginService,
    SandboxResult,
    network_permits,
    run_in_sandbox,
    sanitize_environment,
)

_NOW = datetime(2026, 7, 24, 12, tzinfo=UTC)
_ORG_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_PLUGIN_ID = uuid.uuid4()

# Sentinel secret values the polluted host environment carries; if any of these
# strings surfaces inside the sandboxed child, the boundary has leaked.
_SECRET_ENV = {
    "DATABASE_URL": "postgresql://app:hunter2@db.internal:5432/xiosync",
    "XIOSYNC_AUTH_SECRET": "top-secret-signing-key",
    "AWS_SECRET_ACCESS_KEY": "top-secret-aws-key",
    "WORKER_CREDENTIAL_KEY": "top-secret-worker-key",
    "SESSION_COOKIE": "top-secret-session",
}


def _context() -> OrgContext:
    return OrgContext(
        auth_identity_id=uuid.uuid4(),
        actor_id=_ACTOR_ID,
        organization_id=_ORG_ID,
        session_id=uuid.uuid4(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )


# ---------------------------------------------------------------------------
# A real dummy plugin: a self-contained, self-executing script that reports its
# own sandbox view (environment + working directory) back over stdio as JSON.
# ---------------------------------------------------------------------------

_DUMMY_PLUGIN_SOURCE = '''\
#!{interpreter}
import json
import os
import sys

raw = sys.stdin.read()
try:
    request = json.loads(raw) if raw.strip() else {{}}
except Exception:
    request = {{}}

# What can this plugin actually see? A faithful sandbox strips every credential
# and drops the child into a fresh, empty jail.
env = dict(os.environ)
secret_markers = ("hunter2", "top-secret", "postgresql://")
report = {{
    "method": request.get("method"),
    "params": request.get("params"),
    "env_keys": sorted(env.keys()),
    "database_url_present": "DATABASE_URL" in env,
    "leaked_secret_values": sorted(
        key
        for key, value in env.items()
        if any(marker in value for marker in secret_markers)
    ),
    "cwd": os.getcwd(),
    "cwd_entries": sorted(os.listdir(".")),
}}
sys.stdout.write(json.dumps(report))
'''


def _write_dummy_plugin(directory: Path) -> str:
    """Write an executable dummy-plugin script and return its path (entrypoint)."""
    script = directory / "dummy_plugin.py"
    script.write_text(_DUMMY_PLUGIN_SOURCE.format(interpreter=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR | stat.S_IXUSR)
    return str(script)


def _rpc_payload(method: str = "normalize", params: dict[str, Any] | None = None) -> str:
    return json.dumps({"method": method, "params": params or {}}, separators=(",", ":"))


# ===========================================================================
# Property 1 — INV-PLUGIN-1: a plugin has NO ambient access (real subprocess)
# ===========================================================================


class TestNoAmbientAccess:
    """A sandboxed plugin cannot reach the DB, platform secrets, or the host FS."""

    def test_child_environment_is_fully_stripped_of_db_and_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The launched plugin cannot see DATABASE_URL or any platform secret."""
        for key, value in _SECRET_ENV.items():
            monkeypatch.setenv(key, value)
        # A couple of benign, allowlisted vars must still be present so the child
        # can actually run (proving this is an allowlist, not a blanket wipe).
        monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
        monkeypatch.setenv("LANG", "en_US.UTF-8")

        entrypoint = _write_dummy_plugin(tmp_path)
        result = run_in_sandbox(
            [entrypoint], timeout_seconds=15, stdin_payload=_rpc_payload()
        )

        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)

        # The database is unreachable: its URL never reached the child.
        assert report["database_url_present"] is False
        assert "DATABASE_URL" not in report["env_keys"]
        # No platform secret / credential survived the boundary.
        for secret_key in _SECRET_ENV:
            assert secret_key not in report["env_keys"]
        assert report["leaked_secret_values"] == []
        # Only the minimal locale/tooling allowlist got through.
        assert set(report["env_keys"]) <= {
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "LC_NUMERIC",
            "TZ",
            "HOME",
            "TMPDIR",
            "__CF_USER_TEXT_ENCODING",  # Injected by macOS CoreFoundation/posix_spawn
        }

    def test_child_runs_in_fresh_empty_jail_not_the_repo_tree(
        self, tmp_path: Path
    ) -> None:
        """The plugin's cwd is a private, empty jail — not the caller's working tree."""
        entrypoint = _write_dummy_plugin(tmp_path)
        result = run_in_sandbox(
            [entrypoint], timeout_seconds=15, stdin_payload=_rpc_payload()
        )

        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)

        jail = report["cwd"]
        assert os.path.basename(jail).startswith("xiosync-plugin-")
        # A fresh jail contains nothing the caller could exfiltrate by relative path.
        assert report["cwd_entries"] == []
        # HOME/TMPDIR are re-pointed at the jail, not the host home or shared /tmp.
        # The jail is ephemeral: it no longer exists once the invocation returned.
        assert not os.path.exists(jail)

    def test_service_layer_execution_strips_secrets_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Driven through PluginService: an active plugin still gets a stripped env.

        This exercises the boundary exactly as the control plane invokes it —
        ``execute_plugin_rpc`` → ``run_in_sandbox`` → real subprocess — and proves
        no secret leaks through the service seam either (INV-PLUGIN-1).
        """
        for key, value in _SECRET_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))

        entrypoint = _write_dummy_plugin(tmp_path)
        active = _installation_ns(state=INSTALLATION_STATE_ACTIVE)
        plugin = _plugin_ns(entrypoint=entrypoint, timeout_seconds=15)
        session = _FakeSession(
            scalar_results=[active, plugin],
            scalars_results=[["normalize"]],
        )
        service = PluginService(session)

        result = service.execute_plugin_rpc(
            _context(), active.id, method="normalize", params={"n": 1}
        )

        report = result.output
        assert isinstance(report, dict)
        assert report["method"] == "normalize"
        assert report["database_url_present"] is False
        assert report["leaked_secret_values"] == []
        for secret_key in _SECRET_ENV:
            assert secret_key not in report["env_keys"]

    def test_sanitize_environment_is_pure_allowlist(self) -> None:
        """Defense in depth: the sanitizer copies only the safe allowlist across."""
        polluted = {"PATH": "/usr/bin", **_SECRET_ENV, "RANDOM_APP_VAR": "keep?"}
        clean = sanitize_environment(polluted)
        assert clean == {"PATH": "/usr/bin"}
        assert not any(value.startswith("postgresql://") for value in clean.values())


# ===========================================================================
# Property 2 — INV-PLUGIN-4: an empty allowlist denies ALL (never allow-all)
# ===========================================================================

_DESTINATIONS = [
    ("evil.example.com", 443, "https"),
    ("10.0.0.5", 5432, "tcp"),
    ("metadata.google.internal", 80, "http"),
    ("localhost", 6379, "tcp"),
]


class TestEmptyAllowlistDeniesAll:
    """The default network stance is deny-all; there is no allow-everything mode."""

    @pytest.mark.parametrize("host,port,protocol", _DESTINATIONS)
    def test_empty_allowlist_denies_every_destination_domain(
        self, host: str, port: int, protocol: str
    ) -> None:
        """Pure domain predicate: an empty ruleset permits nothing."""
        assert allowlist_default_denies([]) is True
        assert network_allows([], host, port, protocol) is False

    @pytest.mark.parametrize("host,port,protocol", _DESTINATIONS)
    def test_empty_allowlist_denies_every_destination_service(
        self, host: str, port: int, protocol: str
    ) -> None:
        """Service alias: default-deny with no allow-all path."""
        assert network_permits([], host, port, protocol) is False

    def test_empty_allowlist_denies_all_via_service_persistence_seam(self) -> None:
        """``installation_network_permits`` with zero allow rows denies everything."""
        session = _FakeSession(scalars_results=[[]])
        service = PluginService(session)
        assert (
            service.installation_network_permits(
                _context(), uuid.uuid4(), host="evil.example.com", port=443, protocol="https"
            )
            is False
        )

    def test_concrete_rule_permits_only_its_exact_destination(self) -> None:
        """A single concrete rule opens exactly one triple — nothing wider."""
        rule = NetworkAllowRule(host="api.internal", port=443, protocol="https")
        assert network_allows([rule], "api.internal", 443, "https") is True
        # Any deviation in host, port, or protocol is denied (no wildcarding).
        assert network_allows([rule], "api.internal", 8443, "https") is False
        assert network_allows([rule], "other.internal", 443, "https") is False
        assert network_allows([rule], "api.internal", 443, "http") is False

    @pytest.mark.parametrize(
        "sentinel", ["*", "0.0.0.0", "0.0.0.0/0", "::", "::/0", "any", "all", ""]  # noqa: S104
    )
    def test_allow_all_sentinels_are_rejected_at_construction(self, sentinel: str) -> None:
        """INV-PLUGIN-4: no single rule may smuggle in an 'allow everything' policy."""
        with pytest.raises(ValueError):
            NetworkAllowRule(host=sentinel, port=443, protocol="https")


# ===========================================================================
# Property 3 — INV-PLUGIN-3: no install / activate / execute without approval
# ===========================================================================


class _FakeSessionService:
    """Minimal SessionService stand-in: authenticates every request as CONTEXT."""

    def __init__(self, context: OrgContext) -> None:
        self._context = context

    def validate_access_token(self, access_token: str, *, now: datetime) -> OrgContext:
        del access_token, now
        return self._context


@pytest.fixture
def plugin_api(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, Any]]:
    """A TestClient over the real plugins router with a primed fake DB session.

    The auth middleware's tenant gate (``org_scoped_session``) is replaced with a
    fake that yields whatever ``holder['session']`` a test primes just before the
    request, letting us drive the real router/service branch logic without a DB.
    """
    context = _context()
    holder: dict[str, Any] = {"session": None}

    @contextmanager
    def fake_scope(engine: Any, ctx: OrgContext) -> Iterator[Any]:
        del engine
        assert ctx == context
        yield holder["session"]

    monkeypatch.setattr("xiosync.api.middleware.org_scoped_session", fake_scope)
    app = create_app(
        session_service=_FakeSessionService(context),  # type: ignore[arg-type]
        engine=object(),  # type: ignore[arg-type]
        clock=FixedClock(_NOW),
        max_body_bytes=1_000_000,
    )
    return TestClient(app), holder


_AUTH = {"Authorization": "Bearer test-token"}
_NON_ACTIVE_STATES = [
    INSTALLATION_STATE_PENDING_APPROVAL,
    INSTALLATION_STATE_APPROVED,
    INSTALLATION_STATE_SUSPENDED,
    INSTALLATION_STATE_REVOKED,
]


class TestApprovalGateOverHttp:
    """The router refuses every unapproved path and only runs a truly active install."""

    def test_install_endpoint_can_only_create_pending_approval(
        self, plugin_api: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """INV-PLUGIN-3: install lands in pending_approval — never approved/active."""
        client, holder = plugin_api
        # plugin exists, and there is no prior installation for it.
        holder["session"] = _FakeSession(scalar_results=[_plugin_ns(), None])
        requested_by = uuid.uuid4()

        response = client.post(
            f"/api/v1/plugins/{_PLUGIN_ID}/install",
            headers=_AUTH,
            json={"requested_by": str(requested_by)},
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["state"] == INSTALLATION_STATE_PENDING_APPROVAL
        assert body["approved_by"] is None
        assert body["grant_id"] is None
        assert body["requested_by"] == str(requested_by)

    @pytest.mark.parametrize("state", _NON_ACTIVE_STATES)
    def test_execute_refused_for_non_active_install_and_never_spawns(
        self,
        state: str,
        plugin_api: tuple[TestClient, dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """INV-PLUGIN-3: a non-active install is a 409 *before* any process spawns."""
        client, holder = plugin_api
        holder["session"] = _FakeSession(scalar_results=[_installation_ns(state=state)])

        def _must_not_spawn(*_args: Any, **_kwargs: Any) -> SandboxResult:
            raise AssertionError(
                "run_in_sandbox must never be called for a non-active install"
            )

        monkeypatch.setattr(plugins_module, "run_in_sandbox", _must_not_spawn)

        response = client.post(
            f"/api/v1/plugins/installations/{uuid.uuid4()}/rpc",
            headers=_AUTH,
            json={"method": "normalize", "params": {}},
        )

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "plugin_not_operational"

    def test_activate_refused_for_pending_install(
        self, plugin_api: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """INV-PLUGIN-3: a pending install cannot jump the queue to active."""
        client, holder = plugin_api
        holder["session"] = _FakeSession(
            scalar_results=[_installation_ns(state=INSTALLATION_STATE_PENDING_APPROVAL)]
        )

        response = client.post(
            f"/api/v1/plugins/installations/{uuid.uuid4()}/activate",
            headers=_AUTH,
        )

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "installation_not_activatable"

    def test_execute_succeeds_only_once_active(
        self,
        plugin_api: tuple[TestClient, dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Positive control: the gate is real, not blanket-deny — active runs."""
        client, holder = plugin_api
        active = _installation_ns(state=INSTALLATION_STATE_ACTIVE)
        holder["session"] = _FakeSession(
            scalar_results=[active, _plugin_ns()],
            scalars_results=[["normalize"]],
        )

        monkeypatch.setattr(
            plugins_module,
            "run_in_sandbox",
            lambda *a, **k: SandboxResult(returncode=0, stdout='{"ok": true}', stderr=""),
        )

        response = client.post(
            f"/api/v1/plugins/installations/{active.id}/rpc",
            headers=_AUTH,
            json={"method": "normalize", "params": {}},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"method": "normalize", "output": {"ok": True}}


# ===========================================================================
# Fakes for the DB-free service/router branch tests
# ===========================================================================


class _FakeScalarList:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return list(self._values)


class _FakeSession:
    """A minimal stand-in for the parts of ``Session`` PluginService touches.

    ``scalar`` returns queued values in order; ``scalars`` returns a queued list;
    ``add``/``flush`` are recorded. Mirrors the unit-suite fake so router and
    service branch logic can be driven without a real database.
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


def _plugin_ns(
    *, entrypoint: str = "ghcr.io/acme/plugin@sha256:abc", timeout_seconds: int = 30
) -> Any:
    return SimpleNamespace(
        id=_PLUGIN_ID,
        organization_id=_ORG_ID,
        required_capability_id=uuid.uuid4(),
        entrypoint=entrypoint,
        timeout_seconds=timeout_seconds,
    )


def _installation_ns(*, state: str) -> Any:
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
# Real-PostgreSQL variants (skip without DATABASE_URL; doc 06 §10)
# ===========================================================================

pytest_db = pytest.mark.integration


def _seed_plugin(
    admin_url: str,
    *,
    installation_state: str = INSTALLATION_STATE_PENDING_APPROVAL,
    entrypoint: str = "/bin/true",
    method_name: str = "normalize",
    timeout_seconds: int = 10,
    allow_rules: list[tuple[str, int, str]] | None = None,
) -> dict[str, uuid.UUID]:
    """Seed org + actor + capability + plugin (+ install, + optional allow rules).

    Uses the admin (superuser) channel — which bypasses RLS by design — purely to
    stage fixtures; every *assertion* runs later as the plain application role
    inside ``org_scoped_session`` (doc 05 §3.2). No table DDL is issued
    (INV-TEST-SCHEMA-1); the schema comes only from Alembic.
    """
    ids = {
        "org": new_id(),
        "actor": new_id(),
        "capability": new_id(),
        "plugin": new_id(),
        "installation": new_id(),
    }
    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'Plugin Sandbox Test', 'active')"
                ),
                {"id": ids["org"], "slug": f"plugin-sbx-{ids['org']}"},
            )
            conn.execute(
                text(
                    "INSERT INTO actors (id, organization_id, actor_type, state, "
                    "lifecycle_phase, trust_tier, health_status) VALUES "
                    "(:id, :org, 'human', 'active', 'operational', 'admin', 'healthy')"
                ),
                {"id": ids["actor"], "org": ids["org"]},
            )
            conn.execute(
                text(
                    "INSERT INTO capabilities (id, organization_id, name) "
                    "VALUES (:id, :org, 'plugins.execute')"
                ),
                {"id": ids["capability"], "org": ids["org"]},
            )
            conn.execute(
                text(
                    "INSERT INTO plugins (id, organization_id, name, version, entrypoint, "
                    "required_capability_id, cpu_millis, memory_mb, timeout_seconds, "
                    "filesystem_jail, manifest_hash) VALUES "
                    "(:id, :org, 'dummy', '1.0.0', :entrypoint, :cap, 500, 128, "
                    ":timeout, '/srv/jail', 'sha256:deadbeef')"
                ),
                {
                    "id": ids["plugin"],
                    "org": ids["org"],
                    "entrypoint": entrypoint,
                    "cap": ids["capability"],
                    "timeout": timeout_seconds,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO plugin_rpc_methods (id, organization_id, plugin_id, "
                    "method_name) VALUES (:id, :org, :plugin, :method)"
                ),
                {
                    "id": new_id(),
                    "org": ids["org"],
                    "plugin": ids["plugin"],
                    "method": method_name,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO plugin_installations (id, organization_id, plugin_id, "
                    "state, requested_by) VALUES (:id, :org, :plugin, :state, :actor)"
                ),
                {
                    "id": ids["installation"],
                    "org": ids["org"],
                    "plugin": ids["plugin"],
                    "state": installation_state,
                    "actor": ids["actor"],
                },
            )
            for host, port, protocol in allow_rules or []:
                conn.execute(
                    text(
                        "INSERT INTO plugin_network_allow_rules (id, organization_id, "
                        "installation_id, host, port, protocol) VALUES "
                        "(:id, :org, :install, :host, :port, :protocol)"
                    ),
                    {
                        "id": new_id(),
                        "org": ids["org"],
                        "install": ids["installation"],
                        "host": host,
                        "port": port,
                        "protocol": protocol,
                    },
                )
    finally:
        engine.dispose()
    return ids


def _db_context(ids: dict[str, uuid.UUID]) -> OrgContext:
    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=ids["actor"],
        organization_id=ids["org"],
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )


@pytest_db
class TestSandboxAgainstRealSchema:
    """The same three properties, proven against the real migrated schema."""

    def test_empty_allowlist_denies_all_on_real_schema(
        self, migrated_database_url: str, app_role_database_url: str
    ) -> None:
        """INV-PLUGIN-4: an install with zero allow rows permits no destination."""
        ids = _seed_plugin(migrated_database_url, allow_rules=None)
        ctx = _db_context(ids)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                service = PluginService(session)
                for host, port, protocol in _DESTINATIONS:
                    assert (
                        service.installation_network_permits(
                            ctx, ids["installation"], host=host, port=port, protocol=protocol
                        )
                        is False
                    )
        finally:
            engine.dispose()

    def test_concrete_rule_permits_only_its_destination_on_real_schema(
        self, migrated_database_url: str, app_role_database_url: str
    ) -> None:
        """A persisted concrete rule opens exactly one triple; everything else denied."""
        ids = _seed_plugin(
            migrated_database_url, allow_rules=[("api.internal", 443, "https")]
        )
        ctx = _db_context(ids)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                service = PluginService(session)
                assert service.installation_network_permits(
                    ctx, ids["installation"], host="api.internal", port=443, protocol="https"
                )
                assert not service.installation_network_permits(
                    ctx, ids["installation"], host="api.internal", port=8443, protocol="https"
                )
                assert not service.installation_network_permits(
                    ctx, ids["installation"], host="evil.example.com", port=443, protocol="https"
                )
        finally:
            engine.dispose()

    def test_execute_refused_for_pending_install_on_real_schema(
        self, migrated_database_url: str, app_role_database_url: str
    ) -> None:
        """INV-PLUGIN-3: a pending install cannot execute against the real schema."""
        ids = _seed_plugin(
            migrated_database_url, installation_state=INSTALLATION_STATE_PENDING_APPROVAL
        )
        ctx = _db_context(ids)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                service = PluginService(session)
                with pytest.raises(PluginNotOperationalError):
                    service.execute_plugin_rpc(
                        ctx, ids["installation"], method="normalize", params={}
                    )
        finally:
            engine.dispose()

    def test_activate_refused_for_pending_install_on_real_schema(
        self, migrated_database_url: str, app_role_database_url: str
    ) -> None:
        """INV-PLUGIN-3: activation of a still-pending install is refused."""
        ids = _seed_plugin(
            migrated_database_url, installation_state=INSTALLATION_STATE_PENDING_APPROVAL
        )
        ctx = _db_context(ids)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                service = PluginService(session)
                with pytest.raises(InstallationNotApprovableError):
                    service.activate_installation(ctx, ids["installation"])
        finally:
            engine.dispose()

    def test_full_approval_lifecycle_then_stripped_execution(
        self, migrated_database_url: str, app_role_database_url: str, tmp_path: Path
    ) -> None:
        """End to end: only after approve+activate does a real, stripped plugin run.

        Proves the approval gate (INV-PLUGIN-3) and the stripped-environment
        boundary (INV-PLUGIN-1) compose correctly on the real schema: a
        just-approved, activated install executes a genuine subprocess whose
        environment carries none of the host's secrets.
        """
        entrypoint = _write_dummy_plugin(tmp_path)
        ids = _seed_plugin(
            migrated_database_url,
            installation_state=INSTALLATION_STATE_PENDING_APPROVAL,
            entrypoint=entrypoint,
        )
        ctx = _db_context(ids)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                service = PluginService(session)
                approved = service.approve_installation(
                    ctx, ids["installation"], approved_by=ids["actor"], now=_NOW
                )
                assert approved.state == INSTALLATION_STATE_APPROVED
                assert approved.grant_id is not None
                activated = service.activate_installation(ctx, ids["installation"], now=_NOW)
                assert activated.state == INSTALLATION_STATE_ACTIVE

                result = service.execute_plugin_rpc(
                    ctx, ids["installation"], method="normalize", params={"n": 1}
                )

            report = result.output
            assert isinstance(report, dict)
            assert report["database_url_present"] is False
            assert report["leaked_secret_values"] == []
            assert report["cwd_entries"] == []
        finally:
            engine.dispose()
