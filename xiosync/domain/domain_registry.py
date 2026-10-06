"""Universal Domain Registry — main + dependent auth domain management.

A profile is a residence of multiple domains:
- Independent (self-authenticated): Google, GitHub
- Dependent (authed via parent): v0 (via Google OAuth), Tailscale (via Google OAuth)

The registry tracks domain relationships and supports:
- Auto-detection of new services via cookie domain matching
- Eviction cascade (parent eviction invalidates dependents)
- Not a static list — extensible at runtime via DB

No I/O. No framework imports (RULE-ARCH-1; enforced by import-linter).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DomainRegistration:
    domain_pattern: str
    auth_method: str
    parent_domain: str | None
    cookie_domain_patterns: tuple[str, ...]
    display_name: str | None


DEFAULT_DOMAIN_MAP: dict[str, DomainRegistration] = {
    "google.com": DomainRegistration(
        domain_pattern="google.com",
        auth_method="direct",
        parent_domain=None,
        cookie_domain_patterns=(".google.com", "accounts.google.com", "myaccount.google.com", "mail.google.com", "drive.google.com"),
        display_name="Google",
    ),
    "v0.dev": DomainRegistration(
        domain_pattern="v0.dev",
        auth_method="google_oauth",
        parent_domain="google.com",
        cookie_domain_patterns=(".v0.dev", "v0.dev"),
        display_name="Vercel v0",
    ),
    "tailscale.com": DomainRegistration(
        domain_pattern="tailscale.com",
        auth_method="google_oauth",
        parent_domain="google.com",
        cookie_domain_patterns=(".tailscale.com", "login.tailscale.com"),
        display_name="Tailscale",
    ),
    "github.com": DomainRegistration(
        domain_pattern="github.com",
        auth_method="direct",
        parent_domain=None,
        cookie_domain_patterns=(".github.com", "github.com"),
        display_name="GitHub",
    ),
    "vercel.com": DomainRegistration(
        domain_pattern="vercel.com",
        auth_method="github_oauth",
        parent_domain="github.com",
        cookie_domain_patterns=(".vercel.com", "vercel.com"),
        display_name="Vercel",
    ),
    "proton.me": DomainRegistration(
        domain_pattern="proton.me",
        auth_method="direct",
        parent_domain=None,
        cookie_domain_patterns=(".proton.me", "proton.me", "protonmail.com", ".protonmail.com"),
        display_name="Proton",
    ),
}


def classify_cookie_domain(cookie_domain: str, registry: dict[str, DomainRegistration] | None = None) -> str | None:
    """Map a raw cookie domain to its registered service domain_pattern."""
    reg = registry if registry is not None else DEFAULT_DOMAIN_MAP
    
    # Try exact match first
    for pattern, info in reg.items():
        if cookie_domain in info.cookie_domain_patterns:
            return pattern
            
    # Try suffix match
    for pattern, info in reg.items():
        for c_pattern in info.cookie_domain_patterns:
            if c_pattern.startswith("."):
                if cookie_domain.endswith(c_pattern) or cookie_domain == c_pattern.lstrip("."):
                    return pattern
            else:
                if cookie_domain == c_pattern or cookie_domain.endswith(f".{c_pattern}"):
                    return pattern
    
    return None


def get_dependent_domains(domain_pattern: str, registry: dict[str, DomainRegistration] | None = None) -> list[str]:
    """Return all domains that list this as parent_domain."""
    reg = registry if registry is not None else DEFAULT_DOMAIN_MAP
    return [
        domain for domain, info in reg.items()
        if info.parent_domain == domain_pattern
    ]


def get_auth_chain(domain_pattern: str, registry: dict[str, DomainRegistration] | None = None) -> list[str]:
    """Return the chain of auth dependencies."""
    reg = registry if registry is not None else DEFAULT_DOMAIN_MAP
    chain: list[str] = []
    
    current = domain_pattern
    while True:
        info = reg.get(current)
        if not info or not info.parent_domain:
            break
        current = info.parent_domain
        chain.append(current)
        
    return chain


def should_cascade_eviction(parent: str, child: str, registry: dict[str, DomainRegistration] | None = None) -> bool:
    """Returns True if evicting parent should invalidate child."""
    reg = registry if registry is not None else DEFAULT_DOMAIN_MAP
    child_info = reg.get(child)
    if not child_info:
        return False
    return child_info.parent_domain == parent


def auto_detect_services(cookies: list[dict], registry: dict[str, DomainRegistration] | None = None) -> dict[str, list[dict]]:
    """Group cookies by detected service domain_pattern."""
    result: dict[str, list[dict]] = {}
    
    for cookie in cookies:
        domain = cookie.get("domain")
        if not domain:
            continue
            
        pattern = classify_cookie_domain(domain, registry)
        if not pattern:
            # Synthesize a pattern (apex domain roughly)
            parts = domain.strip(".").split(".")
            if len(parts) >= 2:
                pattern = f"{parts[-2]}.{parts[-1]}"
            else:
                pattern = domain.strip(".")
                
        if pattern not in result:
            result[pattern] = []
        result[pattern].append(cookie)
        
    return result
