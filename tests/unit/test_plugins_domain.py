"""Unit tests for xiosync/domain/plugins.py — pure value objects & predicates.

No database, no fixtures, no I/O. Every model validator and predicate is
exercised; tests are named after the invariant (INV-PLUGIN-1..4) they protect so
a failure message is self-documenting.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from xiosync.domain.plugins import (
    ALLOWED_PROTOCOLS,
    FORBIDDEN_HOST_SENTINELS,
    INSTALLATION_LANDING_STATE,
    INSTALLATION_STATE_ACTIVE,
    INSTALLATION_STATE_APPROVED,
    INSTALLATION_STATE_PENDING_APPROVAL,
    INSTALLATION_STATE_REVOKED,
    INSTALLATION_STATE_SUSPENDED,
    INSTALLATION_STATES,
    PLUGIN_STATE_DEPRECATED,
    PLUGIN_STATE_REGISTERED,
    PLUGIN_STATES,
    NetworkAllowRule,
    PluginManifest,
    ResourceQuota,
    RpcMethodContract,
    allowlist_default_denies,
    installation_can_activate,
    installation_can_be_approved,
    installation_is_operational,
    network_allows,
)

# ---------------------------------------------------------------------------
# Closed vocab / constant sanity
# ---------------------------------------------------------------------------


def test_plugin_states_closed_set() -> None:
    assert PLUGIN_STATES == {PLUGIN_STATE_REGISTERED, PLUGIN_STATE_DEPRECATED}


def test_installation_states_closed_set() -> None:
    assert INSTALLATION_STATES == {
        INSTALLATION_STATE_PENDING_APPROVAL,
        INSTALLATION_STATE_APPROVED,
        INSTALLATION_STATE_ACTIVE,
        INSTALLATION_STATE_SUSPENDED,
        INSTALLATION_STATE_REVOKED,
    }


def test_installation_landing_state_is_pending_approval() -> None:
    """INV-PLUGIN-3: a fresh install lands in pending_approval, never active."""
    assert INSTALLATION_LANDING_STATE == INSTALLATION_STATE_PENDING_APPROVAL


# ---------------------------------------------------------------------------
# ResourceQuota (INV-PLUGIN-1: strictly positive; no unbounded sandbox)
# ---------------------------------------------------------------------------


def test_resource_quota_valid() -> None:
    quota = ResourceQuota(cpu_millis=500, memory_mb=256, timeout_seconds=30)
    assert quota.cpu_millis == 500


@pytest.mark.parametrize(
    "field",
    ["cpu_millis", "memory_mb", "timeout_seconds"],
)
def test_resource_quota_rejects_non_positive(field: str) -> None:
    kwargs = {"cpu_millis": 500, "memory_mb": 256, "timeout_seconds": 30}
    kwargs[field] = 0
    with pytest.raises(ValidationError):
        ResourceQuota(**kwargs)  # type: ignore[arg-type]


def test_resource_quota_is_frozen() -> None:
    quota = ResourceQuota(cpu_millis=1, memory_mb=1, timeout_seconds=1)
    with pytest.raises(ValidationError):
        quota.cpu_millis = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# NetworkAllowRule (INV-PLUGIN-4: concrete destinations only, no allow-all)
# ---------------------------------------------------------------------------


def test_network_allow_rule_valid_and_normalized() -> None:
    rule = NetworkAllowRule(host="API.Example.COM", port=443, protocol="HTTPS")
    # host + protocol are lower-cased so matching is case-insensitive.
    assert rule.host == "api.example.com"
    assert rule.protocol == "https"


@pytest.mark.parametrize("sentinel", sorted(FORBIDDEN_HOST_SENTINELS - {""}))
def test_network_allow_rule_rejects_allow_all_sentinels(sentinel: str) -> None:
    """INV-PLUGIN-4: no single rule may widen the policy to 'everything'."""
    with pytest.raises(ValidationError):
        NetworkAllowRule(host=sentinel, port=443, protocol="https")


def test_network_allow_rule_rejects_empty_host() -> None:
    with pytest.raises(ValidationError):
        NetworkAllowRule(host="", port=443, protocol="https")


@pytest.mark.parametrize("port", [0, -1, 65536, 999999])
def test_network_allow_rule_rejects_out_of_range_port(port: int) -> None:
    with pytest.raises(ValidationError):
        NetworkAllowRule(host="example.com", port=port, protocol="tcp")


def test_network_allow_rule_rejects_unknown_protocol() -> None:
    with pytest.raises(ValidationError):
        NetworkAllowRule(host="example.com", port=53, protocol="carrier-pigeon")


def test_network_allow_rule_matches() -> None:
    rule = NetworkAllowRule(host="example.com", port=443, protocol="https")
    assert rule.matches("EXAMPLE.com", 443, "HTTPS") is True
    assert rule.matches("example.com", 80, "https") is False
    assert rule.matches("other.com", 443, "https") is False
    assert rule.matches("example.com", 443, "tcp") is False


def test_allowed_protocols_are_lowercase() -> None:
    assert all(proto == proto.lower() for proto in ALLOWED_PROTOCOLS)


# ---------------------------------------------------------------------------
# RpcMethodContract (INV-PLUGIN-2: typed I/O)
# ---------------------------------------------------------------------------


def test_rpc_method_contract_valid() -> None:
    method = RpcMethodContract(
        name="fetch",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    assert method.name == "fetch"


def test_rpc_method_contract_requires_name() -> None:
    with pytest.raises(ValidationError):
        RpcMethodContract(name="", input_schema={}, output_schema={})


# ---------------------------------------------------------------------------
# PluginManifest (bundles INV-PLUGIN-1/2/4)
# ---------------------------------------------------------------------------


def _valid_manifest_kwargs() -> dict[str, object]:
    return {
        "name": "csv-normalizer",
        "version": "1.0.0",
        "entrypoint": "ghcr.io/acme/csv-normalizer@sha256:abc",
        "required_capability": "plugin.csv_normalize",
        "filesystem_jail": "/srv/plugins/csv-normalizer",
        "resource_quota": ResourceQuota(
            cpu_millis=500, memory_mb=256, timeout_seconds=30
        ),
        "rpc_methods": [
            RpcMethodContract(
                name="normalize",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            )
        ],
        "network_allowlist": [
            NetworkAllowRule(host="api.acme.com", port=443, protocol="https")
        ],
    }


def test_plugin_manifest_valid() -> None:
    manifest = PluginManifest(**_valid_manifest_kwargs())  # type: ignore[arg-type]
    assert manifest.name == "csv-normalizer"
    assert len(manifest.rpc_methods) == 1


def test_plugin_manifest_allowlist_is_required() -> None:
    """INV-PLUGIN-4: the network allowlist is not optional — omitting it errors."""
    kwargs = _valid_manifest_kwargs()
    del kwargs["network_allowlist"]
    with pytest.raises(ValidationError):
        PluginManifest(**kwargs)  # type: ignore[arg-type]


def test_plugin_manifest_allows_empty_allowlist_as_deny_all() -> None:
    """INV-PLUGIN-4: an explicit empty allowlist is legal (it means deny all)."""
    kwargs = _valid_manifest_kwargs()
    kwargs["network_allowlist"] = []
    manifest = PluginManifest(**kwargs)  # type: ignore[arg-type]
    assert manifest.network_allowlist == []


def test_plugin_manifest_requires_at_least_one_rpc_method() -> None:
    """INV-PLUGIN-2: a plugin with no typed RPC surface is invalid."""
    kwargs = _valid_manifest_kwargs()
    kwargs["rpc_methods"] = []
    with pytest.raises(ValidationError):
        PluginManifest(**kwargs)  # type: ignore[arg-type]


def test_plugin_manifest_rejects_duplicate_rpc_method_names() -> None:
    kwargs = _valid_manifest_kwargs()
    kwargs["rpc_methods"] = [
        RpcMethodContract(name="dup", input_schema={}, output_schema={}),
        RpcMethodContract(name="dup", input_schema={}, output_schema={}),
    ]
    with pytest.raises(ValidationError):
        PluginManifest(**kwargs)  # type: ignore[arg-type]


def test_plugin_manifest_rejects_duplicate_allow_rules() -> None:
    kwargs = _valid_manifest_kwargs()
    kwargs["network_allowlist"] = [
        NetworkAllowRule(host="api.acme.com", port=443, protocol="https"),
        NetworkAllowRule(host="API.ACME.com", port=443, protocol="HTTPS"),
    ]
    with pytest.raises(ValidationError):
        PluginManifest(**kwargs)  # type: ignore[arg-type]


def test_plugin_manifest_rejects_unknown_fields() -> None:
    """extra='forbid': a plugin cannot smuggle an undeclared knob past validation."""
    kwargs = _valid_manifest_kwargs()
    kwargs["ambient_secret"] = "leak"
    with pytest.raises(ValidationError):
        PluginManifest(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Installation lifecycle predicates (INV-PLUGIN-3)
# ---------------------------------------------------------------------------


def test_installation_can_be_approved_only_when_pending() -> None:
    assert installation_can_be_approved(INSTALLATION_STATE_PENDING_APPROVAL) is True
    for state in (
        INSTALLATION_STATE_APPROVED,
        INSTALLATION_STATE_ACTIVE,
        INSTALLATION_STATE_SUSPENDED,
        INSTALLATION_STATE_REVOKED,
    ):
        assert installation_can_be_approved(state) is False


def test_installation_can_activate_only_when_approved() -> None:
    assert installation_can_activate(INSTALLATION_STATE_APPROVED) is True
    assert installation_can_activate(INSTALLATION_STATE_PENDING_APPROVAL) is False
    assert installation_can_activate(INSTALLATION_STATE_ACTIVE) is False


def test_installation_is_operational_only_when_active() -> None:
    assert installation_is_operational(INSTALLATION_STATE_ACTIVE) is True
    for state in (
        INSTALLATION_STATE_PENDING_APPROVAL,
        INSTALLATION_STATE_APPROVED,
        INSTALLATION_STATE_SUSPENDED,
        INSTALLATION_STATE_REVOKED,
    ):
        assert installation_is_operational(state) is False


# ---------------------------------------------------------------------------
# Network allowlist evaluation (INV-PLUGIN-4: default deny, no allow-all)
# ---------------------------------------------------------------------------


def test_empty_allowlist_denies_all() -> None:
    """INV-PLUGIN-4: an empty allowlist permits nothing (never 'allow everything')."""
    assert allowlist_default_denies([]) is True
    assert network_allows([], "example.com", 443, "https") is False


def test_non_empty_allowlist_is_not_default_deny() -> None:
    rules = [NetworkAllowRule(host="example.com", port=443, protocol="https")]
    assert allowlist_default_denies(rules) is False


def test_network_allows_only_explicit_match() -> None:
    rules = [
        NetworkAllowRule(host="api.acme.com", port=443, protocol="https"),
        NetworkAllowRule(host="db.acme.com", port=5432, protocol="tcp"),
    ]
    assert network_allows(rules, "api.acme.com", 443, "https") is True
    assert network_allows(rules, "db.acme.com", 5432, "tcp") is True
    # Case-insensitive host/protocol matching.
    assert network_allows(rules, "API.ACME.COM", 443, "HTTPS") is True
    # Non-matching destinations are denied.
    assert network_allows(rules, "api.acme.com", 8443, "https") is False
    assert network_allows(rules, "evil.example.com", 443, "https") is False
    assert network_allows(rules, "api.acme.com", 443, "tcp") is False
