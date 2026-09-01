"""Sandboxed-plugin domain: manifests, RPC contracts, installs, allowlists.

Pure model layer (RULE-ARCH-1: no I/O, no web/DB framework imports — only
Pydantic value objects and stdlib). Every rule here derives from doc 07 §5 and
DECISIONS.md D-008:

* **INV-PLUGIN-1** — a plugin declares an explicit resource quota, a filesystem
  jail, and a *required capability* Grant. The quota/jail/grant shape is modelled
  here so an invalid manifest is rejected before it is ever persisted.
* **INV-PLUGIN-2** — a plugin exposes a narrow, *typed* host↔plugin RPC; each
  method carries JSON-Schema ``input``/``output`` contracts (``RpcMethodContract``).
* **INV-PLUGIN-3** — installation is *approval-gated*: an install record lands in
  ``pending_approval`` (``INSTALLATION_LANDING_STATE``) and nothing here
  auto-approves it; approval is an explicit, separately-authorized act.
* **INV-PLUGIN-4** — the network allowlist is **not optional** and there is no
  "allow everything" mode. ``PluginManifest.network_allowlist`` is a *required*
  field (an empty list is a legal, fully-closed policy); allow-all host sentinels
  (``*``, ``0.0.0.0/0``, ``::/0``, …) are rejected at construction; and
  :func:`network_allows` is default-deny — an empty ruleset permits nothing.

These are framework-free value objects and pure predicates; the service/host
execution layer (a later Phase 5 step) consumes them. No process spawning,
sandbox wiring, or persistence lives here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Plugin manifest lifecycle (doc 07 §5)
# ---------------------------------------------------------------------------

#: A registered plugin manifest's lifecycle. A corrected/new manifest is a new
#: (name, version) row, never an in-place edit of a registered one.
PLUGIN_STATE_REGISTERED = "registered"
PLUGIN_STATE_DEPRECATED = "deprecated"
PLUGIN_STATES: frozenset[str] = frozenset(
    {PLUGIN_STATE_REGISTERED, PLUGIN_STATE_DEPRECATED}
)


# ---------------------------------------------------------------------------
# Installation lifecycle (INV-PLUGIN-3: approval-gated)
# ---------------------------------------------------------------------------

#: An installation is *requested* and lands here; nothing auto-advances it.
INSTALLATION_STATE_PENDING_APPROVAL = "pending_approval"
#: An authorized approver has granted the required capability to the install.
INSTALLATION_STATE_APPROVED = "approved"
#: The plugin host may launch the plugin for this org.
INSTALLATION_STATE_ACTIVE = "active"
#: Temporarily disabled; may return to ``active`` or be revoked.
INSTALLATION_STATE_SUSPENDED = "suspended"
#: Terminal: the install is withdrawn and cannot execute.
INSTALLATION_STATE_REVOKED = "revoked"

INSTALLATION_STATES: frozenset[str] = frozenset(
    {
        INSTALLATION_STATE_PENDING_APPROVAL,
        INSTALLATION_STATE_APPROVED,
        INSTALLATION_STATE_ACTIVE,
        INSTALLATION_STATE_SUSPENDED,
        INSTALLATION_STATE_REVOKED,
    }
)

#: INV-PLUGIN-3: a fresh installation lands in ``pending_approval``. Both the
#: model default and the service layer read this constant so they cannot drift.
INSTALLATION_LANDING_STATE = INSTALLATION_STATE_PENDING_APPROVAL


# ---------------------------------------------------------------------------
# Network allowlist vocabulary (INV-PLUGIN-4)
# ---------------------------------------------------------------------------

#: The closed set of protocols an allow-rule may name. Deliberately small; a new
#: protocol is a reviewed schema change, not an arbitrary string.
ALLOWED_PROTOCOLS: frozenset[str] = frozenset({"tcp", "udp", "http", "https", "tls"})

#: Host strings that would smuggle an "allow everything" policy past a per-host
#: allowlist. INV-PLUGIN-4 forbids them: an allowlist entry MUST name a concrete
#: destination. An *empty* allowlist is the way to express "deny all" — never a
#: wildcard entry.
FORBIDDEN_HOST_SENTINELS: frozenset[str] = frozenset(
    {"", "*", "any", "all", "0.0.0.0", "0.0.0.0/0", "::", "::/0"}  # noqa: S104
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    """Frozen, extra-forbidding base for every plugin value object.

    ``extra="forbid"`` makes an unrecognised manifest field an error (a plugin
    cannot smuggle an undeclared knob past validation); ``frozen=True`` makes
    these hashable, comparable, side-effect-free value objects.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceQuota(_Strict):
    """CPU / memory / wall-clock ceilings for a sandboxed plugin (INV-PLUGIN-1).

    Every field is strictly positive: a quota of zero (or a missing quota) would
    describe an unbounded sandbox, which is exactly the ambient-access failure
    the sandbox exists to prevent.
    """

    cpu_millis: int = Field(gt=0, description="CPU budget per invocation, in milli-cores.")
    memory_mb: int = Field(gt=0, description="Hard memory ceiling, in mebibytes.")
    timeout_seconds: int = Field(gt=0, description="Wall-clock kill deadline, in seconds.")


class NetworkAllowRule(_Strict):
    """One explicitly-allowed network destination (INV-PLUGIN-4).

    A rule names a concrete ``host``, an explicit ``port``, and a ``protocol``
    from :data:`ALLOWED_PROTOCOLS`. Allow-all host sentinels are rejected so no
    single rule can widen the policy to "everything".
    """

    host: str = Field(min_length=1, description="Concrete hostname or IP literal.")
    port: int = Field(ge=1, le=65535, description="Explicit destination port.")
    protocol: str = Field(description="One of ALLOWED_PROTOCOLS.")

    @field_validator("host")
    @classmethod
    def _reject_allow_all(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in FORBIDDEN_HOST_SENTINELS:
            raise ValueError(
                f"host {value!r} is an allow-all sentinel; the network allowlist "
                "must name concrete destinations (INV-PLUGIN-4). Use an empty "
                "allowlist to deny all network access."
            )
        return normalized

    @field_validator("protocol")
    @classmethod
    def _known_protocol(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ALLOWED_PROTOCOLS:
            raise ValueError(
                f"protocol {value!r} is not one of {sorted(ALLOWED_PROTOCOLS)}"
            )
        return normalized

    def matches(self, host: str, port: int, protocol: str) -> bool:
        """Return ``True`` iff this rule permits ``(host, port, protocol)``."""
        return (
            self.host == host.strip().lower()
            and self.port == port
            and self.protocol == protocol.strip().lower()
        )


class RpcMethodContract(_Strict):
    """A single typed host↔plugin RPC method contract (INV-PLUGIN-2).

    ``input_schema`` / ``output_schema`` are JSON-Schema objects the host uses to
    validate a call's arguments and the plugin's reply. Both are required: an
    unvalidated RPC surface is the in-process-RCE shape C10 remediates.
    """

    name: str = Field(min_length=1, description="RPC method name, unique within a plugin.")
    input_schema: dict[str, Any] = Field(description="JSON Schema for the call arguments.")
    output_schema: dict[str, Any] = Field(description="JSON Schema for the reply payload.")


class PluginManifest(_Strict):
    """A plugin's declared contract, validated before it is ever persisted.

    Bundles everything doc 07 §5 requires a plugin to declare up front: identity
    (``name``/``version``), the process ``entrypoint`` the host launches, the
    ``required_capability`` Grant that gates it (INV-PLUGIN-1), its ``rpc_methods``
    (INV-PLUGIN-2), its ``resource_quota`` and ``filesystem_jail`` (INV-PLUGIN-1),
    and its ``network_allowlist`` (INV-PLUGIN-4, a *required* field).
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1, description="Manifest version (e.g. semver).")
    description: str | None = None
    entrypoint: str = Field(
        min_length=1,
        description="Opaque launch reference (image ref / module) for the sandbox host.",
    )
    required_capability: str = Field(
        min_length=1,
        description="Name of the capability whose Grant authorizes this plugin (INV-PLUGIN-1).",
    )
    filesystem_jail: str = Field(
        min_length=1,
        description="Root of the plugin's filesystem jail (INV-PLUGIN-1).",
    )
    resource_quota: ResourceQuota
    rpc_methods: list[RpcMethodContract] = Field(
        min_length=1,
        description="At least one typed RPC method (INV-PLUGIN-2).",
    )
    # INV-PLUGIN-4: REQUIRED (no default). An empty list is a legal fully-closed
    # policy; the absence of the field is a manifest error, so a plugin can never
    # ship without an explicit network stance.
    network_allowlist: list[NetworkAllowRule] = Field(
        description="Explicit network allowlist; empty = deny all (INV-PLUGIN-4)."
    )

    @field_validator("rpc_methods")
    @classmethod
    def _unique_method_names(
        cls, methods: list[RpcMethodContract]
    ) -> list[RpcMethodContract]:
        names = [method.name for method in methods]
        if len(names) != len(set(names)):
            raise ValueError("rpc method names must be unique within a plugin manifest")
        return methods

    @model_validator(mode="after")
    def _no_duplicate_allow_rules(self) -> PluginManifest:
        seen: set[tuple[str, int, str]] = set()
        for rule in self.network_allowlist:
            key = (rule.host, rule.port, rule.protocol)
            if key in seen:
                raise ValueError(
                    f"duplicate network allow rule {key!r} in plugin manifest"
                )
            seen.add(key)
        return self


# ---------------------------------------------------------------------------
# Installation lifecycle predicates (INV-PLUGIN-3)
# ---------------------------------------------------------------------------


def installation_can_be_approved(state: str) -> bool:
    """Return ``True`` iff an install may transition to ``approved``.

    INV-PLUGIN-3: only a ``pending_approval`` record can be approved. Anything
    already approved/active/suspended/revoked cannot be (re-)approved here; a
    re-approval would need an explicit, separately-modelled re-open.
    """
    return state == INSTALLATION_STATE_PENDING_APPROVAL


def installation_can_activate(state: str) -> bool:
    """Return ``True`` iff an install may transition ``approved`` → ``active``.

    Activation is only legal once approval has granted the required capability
    (INV-PLUGIN-1/3); a ``pending_approval`` install can never be activated.
    """
    return state == INSTALLATION_STATE_APPROVED


def installation_is_operational(state: str) -> bool:
    """Return ``True`` iff the plugin host may launch this install.

    Only an ``active`` install is operational. A ``suspended`` or ``revoked``
    install must not execute even if it was previously approved.
    """
    return state == INSTALLATION_STATE_ACTIVE


# ---------------------------------------------------------------------------
# Network allowlist predicates (INV-PLUGIN-4: default deny, no allow-all)
# ---------------------------------------------------------------------------


def allowlist_default_denies(rules: Sequence[NetworkAllowRule]) -> bool:
    """Return ``True`` iff the ruleset is empty (a fully-closed, deny-all policy).

    Documents the INV-PLUGIN-4 contract explicitly: an empty allowlist means
    *deny all*, never *allow all*.
    """
    return len(rules) == 0


def network_allows(
    rules: Sequence[NetworkAllowRule],
    host: str,
    port: int,
    protocol: str,
) -> bool:
    """Return ``True`` iff some rule explicitly permits ``(host, port, protocol)``.

    Default-deny (INV-PLUGIN-4): with an empty ``rules`` sequence this always
    returns ``False``. Access is granted only when a concrete rule matches; there
    is no wildcard path, because allow-all host sentinels are rejected when a
    :class:`NetworkAllowRule` is constructed.
    """
    return any(rule.matches(host, port, protocol) for rule in rules)
