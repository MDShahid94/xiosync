"""Hierarchical Network Binding — 4-layer priority system.

Priority (highest to lowest):
  4. Workflow IP Binding    — overrides all for in-scope entities
  3. Domain-Specific IP     — per-domain routing within a browser
  2. Profile IP Binding     — default for a profile's browser traffic
  1. Runtime IP Binding     — base network for system tools

No I/O. No framework imports (RULE-ARCH-1; enforced by import-linter).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class NetworkLayer(StrEnum):
    RUNTIME = "runtime"
    PROFILE = "profile"
    DOMAIN = "domain"
    WORKFLOW = "workflow"


class SlotAcquisitionPolicy(StrEnum):
    EXCLUSIVE = "exclusive"
    SHARED = "shared"
    PROFILE_BOUND = "profile_bound"


@dataclass(frozen=True)
class NetworkBindingRule:
    layer: NetworkLayer
    proxy_url: str
    ppp_slot: int | None = None
    domain_pattern: str | None = None
    identity_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None


@dataclass(frozen=True)
class DomainProxyRule:
    domain_pattern: str
    proxy_url: str
    priority: int = 0


@dataclass(frozen=True)
class ProfileExitBinding:
    identity_id: uuid.UUID
    host_id: uuid.UUID
    ppp_slot: int
    proxy_url: str
    public_ip: str | None = None
    is_sticky: bool = True


@dataclass(frozen=True)
class WorkflowNetworkScope:
    workflow_id: uuid.UUID
    dedicated_slot: int | None
    proxy_url: str
    slot_policy: SlotAcquisitionPolicy = SlotAcquisitionPolicy.EXCLUSIVE
    profile_overrides: dict[uuid.UUID, NetworkBindingRule] = field(default_factory=dict)
    domain_overrides: dict[str, NetworkBindingRule] = field(default_factory=dict)


def _domain_matches(domain: str, pattern: str) -> bool:
    """Check if domain matches pattern (exact or suffix match with dot prefix)."""
    if domain == pattern:
        return True
    if pattern.startswith("."):
        return domain.endswith(pattern) or domain == pattern.lstrip(".")
    return domain.endswith(f".{pattern}")


def resolve_effective_proxy(
    identity_id: uuid.UUID | None,
    domain: str | None,
    workflow_scope: WorkflowNetworkScope | None,
    profile_binding: ProfileExitBinding | None,
    domain_rules: list[DomainProxyRule] | None,
    runtime_default: str | None,
) -> str:
    """Resolve the effective proxy URL by walking the 4-layer hierarchy."""

    # Layer 4: Workflow Binding
    if workflow_scope:
        if domain and workflow_scope.domain_overrides:
            for pattern, rule in workflow_scope.domain_overrides.items():
                if _domain_matches(domain, pattern):
                    return rule.proxy_url
        if identity_id and identity_id in workflow_scope.profile_overrides:
            return workflow_scope.profile_overrides[identity_id].proxy_url
        if workflow_scope.proxy_url:
            return workflow_scope.proxy_url

    # Layer 3: Domain-Specific Binding
    if domain and domain_rules:
        # Sort by priority (highest first), then match
        sorted_rules = sorted(domain_rules, key=lambda x: x.priority, reverse=True)
        for rule in sorted_rules:
            if _domain_matches(domain, rule.domain_pattern):
                return rule.proxy_url

    # Layer 2: Profile IP Binding
    if profile_binding:
        return profile_binding.proxy_url

    # Layer 1: Runtime IP Binding
    if runtime_default:
        return runtime_default

    return "DIRECT"


def build_subprocess_env(
    proxy_url: str, no_proxy: str = "localhost,127.0.0.1,100.64.0.0/10"
) -> dict[str, str]:
    """Build environment variables dict for subprocess isolation."""
    if proxy_url == "DIRECT":
        return {}

    url = proxy_url
    if proxy_url.startswith("socks5://"):
        # Upgrade to socks5h to resolve DNS through proxy
        url = proxy_url.replace("socks5://", "socks5h://", 1)

    return {
        "ALL_PROXY": url,
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "NO_PROXY": no_proxy,
        "all_proxy": url,
        "http_proxy": url,
        "https_proxy": url,
        "no_proxy": no_proxy,
    }
