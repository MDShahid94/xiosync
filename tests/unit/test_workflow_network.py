"""Unit tests for xiosync.domain.workflow_network (4-layer hierarchical binding)."""

from __future__ import annotations

import uuid

from xiosync.domain.workflow_network import (
    DomainProxyRule,
    ProfileExitBinding,
    SlotAcquisitionPolicy,
    WorkflowNetworkScope,
    _domain_matches,
    build_subprocess_env,
    resolve_effective_proxy,
)

_IDENTITY = uuid.uuid4()
_WORKFLOW = uuid.uuid4()
_HOST = uuid.uuid4()


class TestDomainMatches:
    def test_exact_match(self):
        assert _domain_matches("v0.dev", "v0.dev") is True

    def test_suffix_match(self):
        assert _domain_matches("api.v0.dev", "v0.dev") is True

    def test_no_match(self):
        assert _domain_matches("google.com", "v0.dev") is False

    def test_partial_not_suffix(self):
        assert _domain_matches("notv0.dev", "v0.dev") is False


class TestResolveEffectiveProxy:
    def test_runtime_default_when_nothing_configured(self):
        result = resolve_effective_proxy(
            identity_id=None,
            domain=None,
            workflow_scope=None,
            profile_binding=None,
            domain_rules=None,
            runtime_default="socks5://rt:1080",
        )
        assert result == "socks5://rt:1080"

    def test_returns_direct_when_nothing_at_all(self):
        result = resolve_effective_proxy(
            identity_id=None,
            domain=None,
            workflow_scope=None,
            profile_binding=None,
            domain_rules=None,
            runtime_default=None,
        )
        assert result == "DIRECT"

    def test_profile_overrides_runtime(self):
        binding = ProfileExitBinding(
            identity_id=_IDENTITY,
            host_id=_HOST,
            ppp_slot=17,
            proxy_url="socks5://vm:10017",
            public_ip="1.2.3.4",
        )
        result = resolve_effective_proxy(
            identity_id=_IDENTITY,
            domain=None,
            workflow_scope=None,
            profile_binding=binding,
            domain_rules=None,
            runtime_default="socks5://rt:1080",
        )
        assert result == "socks5://vm:10017"

    def test_domain_overrides_profile(self):
        binding = ProfileExitBinding(
            identity_id=_IDENTITY,
            host_id=_HOST,
            ppp_slot=17,
            proxy_url="socks5://vm:10017",
            public_ip="1.2.3.4",
        )
        domain_rules = [
            DomainProxyRule(domain_pattern="v0.dev", proxy_url="socks5://vm:10042", priority=10),
        ]
        result = resolve_effective_proxy(
            identity_id=_IDENTITY,
            domain="v0.dev",
            workflow_scope=None,
            profile_binding=binding,
            domain_rules=domain_rules,
            runtime_default=None,
        )
        assert result == "socks5://vm:10042"

    def test_domain_rule_fallback_to_profile(self):
        binding = ProfileExitBinding(
            identity_id=_IDENTITY,
            host_id=_HOST,
            ppp_slot=17,
            proxy_url="socks5://vm:10017",
            public_ip="1.2.3.4",
        )
        domain_rules = [
            DomainProxyRule(domain_pattern="v0.dev", proxy_url="socks5://vm:10042", priority=10),
        ]
        # github.com doesn't match v0.dev rule, falls back to profile
        result = resolve_effective_proxy(
            identity_id=_IDENTITY,
            domain="github.com",
            workflow_scope=None,
            profile_binding=binding,
            domain_rules=domain_rules,
            runtime_default=None,
        )
        assert result == "socks5://vm:10017"

    def test_workflow_overrides_everything(self):
        binding = ProfileExitBinding(
            identity_id=_IDENTITY,
            host_id=_HOST,
            ppp_slot=17,
            proxy_url="socks5://vm:10017",
            public_ip="1.2.3.4",
        )
        domain_rules = [
            DomainProxyRule(domain_pattern="v0.dev", proxy_url="socks5://vm:10042", priority=10),
        ]
        workflow = WorkflowNetworkScope(
            workflow_id=_WORKFLOW,
            dedicated_slot=99,
            proxy_url="socks5://vm:10099",
        )
        result = resolve_effective_proxy(
            identity_id=_IDENTITY,
            domain="v0.dev",
            workflow_scope=workflow,
            profile_binding=binding,
            domain_rules=domain_rules,
            runtime_default="socks5://rt:1080",
        )
        assert result == "socks5://vm:10099"

    def test_domain_priority_ordering(self):
        rules = [
            DomainProxyRule(domain_pattern="v0.dev", proxy_url="socks5://low:1", priority=1),
            DomainProxyRule(domain_pattern="v0.dev", proxy_url="socks5://high:2", priority=10),
        ]
        result = resolve_effective_proxy(
            identity_id=_IDENTITY,
            domain="v0.dev",
            workflow_scope=None,
            profile_binding=None,
            domain_rules=rules,
            runtime_default=None,
        )
        assert result == "socks5://high:2"


class TestBuildSubprocessEnv:
    def test_sets_all_proxy_vars(self):
        env = build_subprocess_env("socks5://vm:10017")
        assert "ALL_PROXY" in env
        assert "HTTP_PROXY" in env
        assert "HTTPS_PROXY" in env
        assert "NO_PROXY" in env

    def test_uses_socks5h_for_remote_dns(self):
        env = build_subprocess_env("socks5://vm:10017")
        assert "socks5h://" in env["ALL_PROXY"]

    def test_no_proxy_includes_tailscale(self):
        env = build_subprocess_env("socks5://vm:10017")
        assert "100.64.0.0" in env["NO_PROXY"]


class TestSlotAcquisitionPolicy:
    def test_enum_values(self):
        assert SlotAcquisitionPolicy.EXCLUSIVE == "exclusive"
        assert SlotAcquisitionPolicy.SHARED == "shared"
        assert SlotAcquisitionPolicy.PROFILE_BOUND == "profile_bound"
