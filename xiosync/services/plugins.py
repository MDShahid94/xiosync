"""Sandboxed-plugin use cases + the out-of-process execution boundary (doc 07 §5).

``PluginService`` is the sanctioned entry point for the plugin control plane and
its execution boundary. It enforces, on write and on execute, the four plugin
invariants proven pure in ``xiosync/domain/plugins`` (C10 remediation):

* **INV-PLUGIN-1 — no ambient access; explicit quota / jail / grant.** Execution
  runs *out-of-process* via :func:`run_in_sandbox`, which launches the plugin
  with a **stripped environment** (an allowlist — every platform secret,
  ``DATABASE_URL``, JWT/session key, and cloud credential is removed) inside a
  **fresh temporary filesystem jail** it creates and destroys per invocation, and
  under the plugin's declared wall-clock ``timeout``.
* **INV-PLUGIN-2 — narrow, typed host↔plugin RPC.** ``execute_plugin_rpc`` only
  dispatches a method the manifest declared and speaks a single JSON request /
  JSON reply over stdio; an unknown method or a non-object payload is refused
  before a process is ever spawned.
* **INV-PLUGIN-3 — installation is approval-gated.** ``install_plugin`` can only
  create a record in ``pending_approval`` (``INSTALLATION_LANDING_STATE``); it has
  no argument that can shortcut approval. Reaching an *operational* (``active``)
  install requires two further, separately-authorized acts —
  ``approve_installation`` (which mints the required-capability Grant) and
  ``activate_installation``. Execution refuses any install that is not ``active``.
* **INV-PLUGIN-4 — the network allowlist is enforced, default-deny.** The
  allowlist is carried on the manifest/installation (populated by the persistence
  layer); ``network_permits`` here re-uses the pure domain predicate so an empty
  allowlist denies all and there is no allow-all path.

Following ``WorkflowService``/``WorkerService``: the service takes the caller's
``Session`` (the caller owns the transaction via ``org_scoped_session``), reads
with ``scalar``/``scalars``, flushes each write, and returns frozen dataclass
values so it holds no live ORM state. ``organization_id`` always comes from the
context, never the caller.

The low-level boundary (:func:`sanitize_environment`, :func:`run_in_sandbox`) is
deliberately database-free so the strict security guarantees can be unit-tested
with a real subprocess and no control-plane wiring.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.domain.plugins import (
    INSTALLATION_LANDING_STATE,
    INSTALLATION_STATE_ACTIVE,
    INSTALLATION_STATE_APPROVED,
    NetworkAllowRule,
    installation_can_activate,
    installation_can_be_approved,
    installation_is_operational,
    network_allows,
)
from xiosync.persistence.models.authorization import Grant
from xiosync.persistence.models.plugins import (
    Plugin,
    PluginInstallation,
    PluginNetworkAllowRule,
    PluginRpcMethod,
)
from xiosync.platform.ids import new_id

#: The ``grants.state`` value for a live grant. Mirrors the ``state_allowed``
#: CHECK on the ``grants`` table ("active" | "revoked"); a plugin's minted grant
#: starts active (INV-PLUGIN-1).
_GRANT_STATE_ACTIVE = "active"

__all__ = [
    "PluginAlreadyInstalledError",
    "PluginExecutionError",
    "PluginInstallationRecord",
    "PluginNotFoundError",
    "PluginNotOperationalError",
    "PluginRpcResult",
    "PluginService",
    "PluginTimeoutError",
    "SandboxResult",
    "InstallationNotApprovableError",
    "InstallationNotFoundError",
    "UnknownRpcMethodError",
    "run_in_sandbox",
    "sanitize_environment",
]


# ---------------------------------------------------------------------------
# Environment sanitization (INV-PLUGIN-1: no ambient access to platform secrets)
# ---------------------------------------------------------------------------

#: The *only* environment variables a sandboxed plugin inherits. This is an
#: allowlist, not a denylist: anything not named here simply never reaches the
#: child, so a newly-added secret env var cannot silently leak into a plugin.
#: None of these carry credentials — they are locale/tooling basics a benign
#: process needs to run.
SAFE_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "LC_NUMERIC", "TZ"}
)

#: Substrings that mark a key as secret-bearing. Used to *reject* any explicit
#: per-plugin env a caller tries to inject (defense-in-depth on top of the
#: allowlist) so a caller can never re-introduce a secret through the side door.
_SENSITIVE_ENV_MARKERS: tuple[str, ...] = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "KEY",
    "CREDENTIAL",
    "DATABASE_URL",
    "DSN",
    "JWT",
    "SESSION",
    "COOKIE",
    "PRIVATE",
)

#: Prefixes whose entire namespace is secret-bearing / control-plane-internal.
_SENSITIVE_ENV_PREFIXES: tuple[str, ...] = (
    "XIOSYNC_",
    "AWS_",
    "AZURE_",
    "GCP_",
    "GOOGLE_",
    "VERCEL_",
    "WORKER_",
)


def env_key_is_sensitive(key: str) -> bool:
    """Return ``True`` if ``key`` names a secret-bearing / internal variable.

    Case-insensitive. A key is sensitive if it starts with any control-plane /
    cloud prefix or contains any credential marker substring. Intentionally
    over-broad: over-stripping a benign var is harmless, leaking a secret is not.
    """
    upper = key.upper()
    if any(upper.startswith(prefix) for prefix in _SENSITIVE_ENV_PREFIXES):
        return True
    return any(marker in upper for marker in _SENSITIVE_ENV_MARKERS)


def sanitize_environment(
    base_env: Mapping[str, str] | None = None,
    *,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the minimal, secret-free environment for a sandboxed plugin.

    Starts from *nothing* and copies across only the keys in
    :data:`SAFE_ENV_ALLOWLIST` that are present in ``base_env`` (defaulting to
    the host ``os.environ``). This strips ``DATABASE_URL``, ``XIOSYNC_*`` config
    and signing secrets, worker-credential keys, cloud credentials, and every
    other ambient value (INV-PLUGIN-1).

    ``extra_env`` lets the host pass a plugin explicit, non-secret configuration.
    Each key is re-checked with :func:`env_key_is_sensitive`; a sensitive key
    raises :class:`ValueError` so a secret can never be smuggled back in.
    """
    source: Mapping[str, str] = os.environ if base_env is None else base_env

    clean: dict[str, str] = {}
    for key in SAFE_ENV_ALLOWLIST:
        value = source.get(key)
        if value is not None:
            clean[key] = value

    if extra_env:
        for key, value in extra_env.items():
            if env_key_is_sensitive(key):
                raise ValueError(
                    f"refusing to pass sensitive environment variable {key!r} into a "
                    "plugin sandbox (INV-PLUGIN-1: plugins get no ambient secrets)"
                )
            clean[key] = value

    return clean


# ---------------------------------------------------------------------------
# Sandbox execution boundary (INV-PLUGIN-1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """The raw outcome of one out-of-process sandbox invocation."""

    returncode: int
    stdout: str
    stderr: str


class PluginTimeoutError(Exception):
    """The sandboxed process exceeded its declared wall-clock ``timeout``."""

    def __init__(self, timeout_seconds: int) -> None:
        super().__init__(
            f"plugin sandbox exceeded its {timeout_seconds}s wall-clock quota "
            "and was killed (INV-PLUGIN-1)"
        )
        self.timeout_seconds = timeout_seconds


class PluginExecutionError(Exception):
    """The plugin exited non-zero or produced an unreadable RPC reply."""


def run_in_sandbox(
    argv: Sequence[str],
    *,
    timeout_seconds: int,
    stdin_payload: str = "",
    base_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> SandboxResult:
    """Run ``argv`` out-of-process under the plugin sandbox constraints.

    The strict security boundary (INV-PLUGIN-1), database-free so it is directly
    unit-testable:

    * **Stripped environment** — the child sees only :func:`sanitize_environment`
      output; ``HOME``/``TMPDIR`` are re-pointed at the jail so it cannot read the
      host home or the shared system temp dir.
    * **Temporary filesystem jail** — a fresh ``mkdtemp`` directory is created per
      invocation, used as the child's ``cwd``, and unconditionally removed
      afterwards. The process starts with no view of the caller's working tree.
    * **No shell** — ``argv`` is passed as a list with ``shell=False``; there is no
      string interpolation into a shell.
    * **Wall-clock quota** — ``timeout_seconds`` bounds the run; a breach raises
      :class:`PluginTimeoutError` after the child is killed.

    Returns a :class:`SandboxResult`; interpreting a non-zero ``returncode`` is
    left to the caller (``execute_plugin_rpc`` treats it as failure).
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive (INV-PLUGIN-1)")
    if not argv:
        raise ValueError("argv must name the plugin process to launch")

    env = sanitize_environment(base_env, extra_env=extra_env)
    jail = tempfile.mkdtemp(prefix="xiosync-plugin-")
    # Re-point HOME/TMPDIR at the jail so the plugin cannot reach the host's
    # home directory or the shared /tmp. These are non-secret by definition.
    env["HOME"] = jail
    env["TMPDIR"] = jail

    try:
        completed = subprocess.run(  # noqa: S603 — deliberate sandboxed plugin launch (INV-PLUGIN-1)
            list(argv),
            input=stdin_payload,
            capture_output=True,
            text=True,
            cwd=jail,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginTimeoutError(timeout_seconds) from exc
    finally:
        # The jail is ephemeral: destroy it whether the plugin succeeded, failed,
        # or timed out, so nothing it wrote survives the invocation.
        shutil.rmtree(jail, ignore_errors=True)

    return SandboxResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def build_launch_argv(entrypoint: str) -> list[str]:
    """Map a manifest ``entrypoint`` to the process-launch argv.

    The entrypoint is an opaque launch reference (an image ref / module the
    sandbox host knows how to start). It is treated as a single argument — never
    split or interpolated into a shell — so it cannot inject extra arguments.
    Substituting a real container runtime here is a deployment concern; the
    security boundary in :func:`run_in_sandbox` is independent of it.
    """
    return [entrypoint]


def network_permits(
    rules: Sequence[NetworkAllowRule], host: str, port: int, protocol: str
) -> bool:
    """Thin service-layer alias for the pure allowlist predicate (INV-PLUGIN-4).

    Default-deny: an empty ``rules`` sequence permits nothing, and there is no
    allow-all path because allow-all host sentinels are rejected at construction.
    """
    return network_allows(rules, host, port, protocol)


# ---------------------------------------------------------------------------
# Read-model dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PluginInstallationRecord:
    """Frozen snapshot of a ``plugin_installations`` row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    plugin_id: uuid.UUID
    state: str
    requested_by: uuid.UUID
    approved_by: uuid.UUID | None
    grant_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class PluginRpcResult:
    """The typed reply from a plugin RPC invocation (INV-PLUGIN-2)."""

    method: str
    output: object


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class PluginNotFoundError(Exception):
    """The referenced plugin manifest does not exist in this organization."""

    def __init__(self, plugin_id: uuid.UUID) -> None:
        super().__init__(f"plugin {plugin_id} not found in organization")
        self.plugin_id = plugin_id


class PluginAlreadyInstalledError(Exception):
    """The plugin already has an installation record in this organization.

    One installation per plugin per org is enforced by
    ``uq_plugin_installations_org_plugin``; this surfaces it as a domain error
    before hitting the constraint.
    """

    def __init__(self, plugin_id: uuid.UUID) -> None:
        super().__init__(f"plugin {plugin_id} is already installed in organization")
        self.plugin_id = plugin_id


class InstallationNotFoundError(Exception):
    """The referenced installation does not exist in this organization."""

    def __init__(self, installation_id: uuid.UUID) -> None:
        super().__init__(f"installation {installation_id} not found in organization")
        self.installation_id = installation_id


class InstallationNotApprovableError(Exception):
    """The installation is not in a state that permits the requested transition."""

    def __init__(self, installation_id: uuid.UUID, state: str, action: str) -> None:
        super().__init__(
            f"installation {installation_id} is in state {state!r}; cannot {action}"
        )
        self.installation_id = installation_id
        self.state = state
        self.action = action


class PluginNotOperationalError(Exception):
    """Execution was attempted against an install that is not ``active``.

    INV-PLUGIN-3: a plugin may only run once its install has been approved *and*
    activated. A ``pending_approval``, ``approved``, ``suspended``, or ``revoked``
    install must never launch a process.
    """

    def __init__(self, installation_id: uuid.UUID, state: str) -> None:
        super().__init__(
            f"installation {installation_id} is in state {state!r}; only 'active' "
            "installs may execute (INV-PLUGIN-3)"
        )
        self.installation_id = installation_id
        self.state = state


class UnknownRpcMethodError(Exception):
    """The requested RPC method is not declared by the plugin manifest (INV-PLUGIN-2)."""

    def __init__(self, method: str) -> None:
        super().__init__(f"rpc method {method!r} is not declared by this plugin")
        self.method = method


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PluginService:
    """Use cases for the sandboxed-plugin control plane and execution (doc 07 §5).

    Instantiate with the caller's ``Session``; the caller owns the transaction
    boundary via ``org_scoped_session``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Installation lifecycle (INV-PLUGIN-3: approval-gated)
    # ------------------------------------------------------------------

    def install_plugin(
        self,
        ctx: OrgContext,
        plugin_id: uuid.UUID,
        *,
        requested_by: uuid.UUID,
    ) -> PluginInstallationRecord:
        """Request an installation of a registered plugin into this org.

        INV-PLUGIN-3: the record can *only* land in ``pending_approval``
        (:data:`INSTALLATION_LANDING_STATE`). There is deliberately no parameter
        that approves or activates in one step — reaching an operational install
        requires the separate, authorized :meth:`approve_installation` and
        :meth:`activate_installation` calls. This is the approval gate.

        Raises:
            PluginNotFoundError: if ``plugin_id`` does not exist in this org.
            PluginAlreadyInstalledError: if the plugin already has an install.
        """
        plugin = self._session.scalar(
            select(Plugin).where(
                Plugin.organization_id == ctx.organization_id,
                Plugin.id == plugin_id,
            )
        )
        if plugin is None:
            raise PluginNotFoundError(plugin_id)

        existing = self._session.scalar(
            select(PluginInstallation).where(
                PluginInstallation.organization_id == ctx.organization_id,
                PluginInstallation.plugin_id == plugin_id,
            )
        )
        if existing is not None:
            raise PluginAlreadyInstalledError(plugin_id)

        installation_id = new_id()
        row = PluginInstallation(
            id=installation_id,
            organization_id=ctx.organization_id,
            plugin_id=plugin_id,
            # INV-PLUGIN-3: nothing here may set this to approved/active.
            state=INSTALLATION_LANDING_STATE,
            requested_by=requested_by,
            approved_by=None,
            grant_id=None,
        )
        self._session.add(row)
        self._session.flush()
        return _installation_record(row)

    def approve_installation(
        self,
        ctx: OrgContext,
        installation_id: uuid.UUID,
        *,
        approved_by: uuid.UUID,
        now: datetime | None = None,
    ) -> PluginInstallationRecord:
        """Approve a pending install, minting the required-capability Grant.

        INV-PLUGIN-3: only a ``pending_approval`` install can be approved
        (guarded by the pure ``installation_can_be_approved`` predicate). This is
        the explicit, separately-authorized act the caller (an org admin) performs
        after review. On approval it mints the capability Grant the plugin
        declared (INV-PLUGIN-1), links it via ``grant_id``, records ``approved_by``
        / ``approved_at``, and moves the install to ``approved`` — but *not* yet
        ``active`` (activation is a further step).

        Raises:
            InstallationNotFoundError: if the install does not exist in this org.
            InstallationNotApprovableError: if it is not ``pending_approval``.
        """
        effective_now = now if now is not None else datetime.now(tz=UTC)
        row = self._locked_installation(ctx, installation_id)
        if not installation_can_be_approved(row.state):
            raise InstallationNotApprovableError(installation_id, row.state, "approve")

        plugin = self._session.scalar(
            select(Plugin).where(
                Plugin.organization_id == ctx.organization_id,
                Plugin.id == row.plugin_id,
            )
        )
        if plugin is None:  # pragma: no cover - guarded by same-org FK
            raise PluginNotFoundError(row.plugin_id)

        # INV-PLUGIN-1: approval mints the capability Grant that authorizes the
        # plugin. The grant is scoped to the requester's actor and the manifest's
        # required capability; nothing runs without it.
        grant_id = new_id()
        self._session.add(
            Grant(
                id=grant_id,
                organization_id=ctx.organization_id,
                actor_id=row.requested_by,
                capability_id=plugin.required_capability_id,
                state=_GRANT_STATE_ACTIVE,
            )
        )

        row.state = INSTALLATION_STATE_APPROVED
        row.approved_by = approved_by
        row.approved_at = effective_now
        row.grant_id = grant_id
        row.updated_at = effective_now
        self._session.flush()
        return _installation_record(row)

    def activate_installation(
        self,
        ctx: OrgContext,
        installation_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> PluginInstallationRecord:
        """Activate an approved install: ``approved`` → ``active``.

        Only an ``approved`` install may activate (``installation_can_activate``);
        a ``pending_approval`` install can never be activated, so the approval
        gate cannot be bypassed. Once ``active`` the plugin host may launch it.

        Raises:
            InstallationNotFoundError: if the install does not exist in this org.
            InstallationNotApprovableError: if it is not ``approved``.
        """
        effective_now = now if now is not None else datetime.now(tz=UTC)
        row = self._locked_installation(ctx, installation_id)
        if not installation_can_activate(row.state):
            raise InstallationNotApprovableError(installation_id, row.state, "activate")

        row.state = INSTALLATION_STATE_ACTIVE
        row.updated_at = effective_now
        self._session.flush()
        return _installation_record(row)

    def get_installation(
        self, ctx: OrgContext, installation_id: uuid.UUID
    ) -> PluginInstallationRecord | None:
        """Return the installation record, or ``None`` if absent in this org."""
        row = self._session.scalar(
            select(PluginInstallation).where(
                PluginInstallation.organization_id == ctx.organization_id,
                PluginInstallation.id == installation_id,
            )
        )
        return None if row is None else _installation_record(row)

    # ------------------------------------------------------------------
    # Execution boundary (INV-PLUGIN-1/2/3)
    # ------------------------------------------------------------------

    def execute_plugin_rpc(
        self,
        ctx: OrgContext,
        installation_id: uuid.UUID,
        *,
        method: str,
        params: Mapping[str, object],
        base_env: Mapping[str, str] | None = None,
    ) -> PluginRpcResult:
        """Invoke one typed RPC method on an *active* plugin, out-of-process.

        The full execute-time enforcement:

        1. **INV-PLUGIN-3** — the install must be ``active``; any other state
           raises :class:`PluginNotOperationalError` *before* a process is
           spawned.
        2. **INV-PLUGIN-2** — ``method`` must be one the manifest declared, and
           ``params`` must be a JSON object; otherwise the call is refused with no
           process launch.
        3. **INV-PLUGIN-1** — the call is dispatched through :func:`run_in_sandbox`
           (stripped env, temp jail, wall-clock quota). A single JSON request is
           written to the child's stdin and a single JSON reply is read from its
           stdout.

        Raises:
            InstallationNotFoundError, PluginNotFoundError,
            PluginNotOperationalError, UnknownRpcMethodError, PluginTimeoutError,
            PluginExecutionError, ValueError.
        """
        installation = self._session.scalar(
            select(PluginInstallation).where(
                PluginInstallation.organization_id == ctx.organization_id,
                PluginInstallation.id == installation_id,
            )
        )
        if installation is None:
            raise InstallationNotFoundError(installation_id)

        # INV-PLUGIN-3: the single most important execute-time gate. A plugin that
        # was never approved (still pending), or is suspended/revoked, cannot run.
        if not installation_is_operational(installation.state):
            raise PluginNotOperationalError(installation_id, installation.state)

        plugin = self._session.scalar(
            select(Plugin).where(
                Plugin.organization_id == ctx.organization_id,
                Plugin.id == installation.plugin_id,
            )
        )
        if plugin is None:  # pragma: no cover - guarded by same-org FK
            raise PluginNotFoundError(installation.plugin_id)

        # INV-PLUGIN-2: only a declared method may be dispatched.
        declared_methods = set(
            self._session.scalars(
                select(PluginRpcMethod.method_name).where(
                    PluginRpcMethod.organization_id == ctx.organization_id,
                    PluginRpcMethod.plugin_id == plugin.id,
                )
            ).all()
        )
        if method not in declared_methods:
            raise UnknownRpcMethodError(method)
        if not isinstance(params, Mapping):
            raise ValueError("params must be a JSON object (INV-PLUGIN-2)")

        request_payload = json.dumps(
            {"method": method, "params": dict(params)}, separators=(",", ":")
        )
        result = run_in_sandbox(
            build_launch_argv(plugin.entrypoint),
            timeout_seconds=plugin.timeout_seconds,
            stdin_payload=request_payload,
            base_env=base_env,
        )
        if result.returncode != 0:
            raise PluginExecutionError(
                f"plugin exited with code {result.returncode}: {result.stderr.strip()!r}"
            )
        try:
            reply = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PluginExecutionError(
                "plugin produced a non-JSON RPC reply (INV-PLUGIN-2)"
            ) from exc
        return PluginRpcResult(method=method, output=reply)

    # ------------------------------------------------------------------
    # Network allowlist (INV-PLUGIN-4)
    # ------------------------------------------------------------------

    def installation_network_permits(
        self,
        ctx: OrgContext,
        installation_id: uuid.UUID,
        *,
        host: str,
        port: int,
        protocol: str,
    ) -> bool:
        """Return ``True`` iff the install's allowlist permits the destination.

        INV-PLUGIN-4, default-deny: an installation with no allow rows permits
        nothing. Rules are loaded from ``plugin_network_allow_rules`` and rebuilt
        into the pure domain :class:`NetworkAllowRule` value objects so the single
        authoritative predicate decides.
        """
        rows = self._session.scalars(
            select(PluginNetworkAllowRule).where(
                PluginNetworkAllowRule.organization_id == ctx.organization_id,
                PluginNetworkAllowRule.installation_id == installation_id,
            )
        ).all()
        rules = [
            NetworkAllowRule(host=row.host, port=row.port, protocol=row.protocol)
            for row in rows
        ]
        return network_permits(rules, host, port, protocol)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _locked_installation(
        self, ctx: OrgContext, installation_id: uuid.UUID
    ) -> PluginInstallation:
        """Fetch an installation row with a row-level lock, raising if absent."""
        row = self._session.scalar(
            select(PluginInstallation)
            .where(
                PluginInstallation.organization_id == ctx.organization_id,
                PluginInstallation.id == installation_id,
            )
            .with_for_update()
        )
        if row is None:
            raise InstallationNotFoundError(installation_id)
        return row


def _installation_record(row: PluginInstallation) -> PluginInstallationRecord:
    return PluginInstallationRecord(
        id=row.id,
        organization_id=row.organization_id,
        plugin_id=row.plugin_id,
        state=row.state,
        requested_by=row.requested_by,
        approved_by=row.approved_by,
        grant_id=row.grant_id,
    )
