"""Pure domain types and operations for Cookie-Injected Profile Identity (CIPI).

A ProfileIdentity is a virtual identity container that:
- Has a stable serial (PRFL-NNN) independent of email/username
- Contains domain-scoped cookie sets (Google, Tailscale, v0, etc.)
- Can be materialized as tar_profile (default/ground truth) or cookie_injection (experimental)
- Supports domain-aware eviction

No I/O. No framework imports (RULE-ARCH-1; enforced by import-linter).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum


class CookieTier(StrEnum):
    PRIMARY = "PRIMARY"
    ROTATING = "ROTATING"
    LOGIN_EVENT = "LOGIN_EVENT"
    PREFERENCE = "PREFERENCE"


class CookieHealthUrgency(StrEnum):
    NONE = "NONE"
    SCHEDULED = "SCHEDULED"
    SOON = "SOON"
    IMMEDIATE = "IMMEDIATE"


class MaterializationMode(StrEnum):
    TAR_PROFILE = "TAR_PROFILE"
    COOKIE_INJECTION = "COOKIE_INJECTION"


@dataclass(frozen=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str
    expires: float
    httpOnly: bool
    secure: bool
    sameSite: str
    size: int | None = None


@dataclass(frozen=True)
class KVPair:
    name: str
    value: str


@dataclass(frozen=True)
class CookieExpiry:
    name: str
    domain: str
    expires_at: float
    remaining_seconds: float
    tier: CookieTier


@dataclass(frozen=True)
class DomainCookieSet:
    domain_pattern: str
    cookies: tuple[BrowserCookie, ...]
    local_storage: dict[str, tuple[KVPair, ...]]
    health: CookieHealthUrgency
    last_verified_at: datetime | None
    is_valid: bool
    auth_method: str | None = None
    parent_domain: str | None = None


@dataclass(frozen=True)
class ProfileIdentity:
    identity_id: uuid.UUID
    serial: int
    materialization: MaterializationMode
    is_persisted: bool
    version: int
    organization_id: uuid.UUID | None = None
    domain_sets: dict[str, DomainCookieSet] = field(default_factory=dict)
    storage_object_key: str | None = None

    @property
    def canonical_key(self) -> str:
        return f"PRFL-{self.serial:03d}"

    @property
    def tar_object_key(self) -> str:
        return f"chrome_profiles/{self.canonical_key}.tar.gz"

    @property
    def state_object_key(self) -> str:
        return f"session_states/{self.canonical_key}.json"


# Constants
MID_AUTH_URL_PATTERNS = (
    "accounts.google.com/o/oauth2",
    "accounts.google.com/signin",
    "accounts.google.com/CheckCookie",
)
GOOGLE_DOMAINS = (
    ".google.com",
    "accounts.google.com",
    "myaccount.google.com",
)
GOOGLE_SID_NAMES = frozenset({"SID", "SSID", "HSID", "APISID", "SAPISID", "SIDCC"})
SID_TTL_THRESHOLD_SEC = 3600.0


def evict_domain(
    profile: ProfileIdentity, domain_pattern: str, cascade_to_dependents: bool = True
) -> ProfileIdentity:
    """Returns a new ProfileIdentity with domain_pattern removed.

    If cascade_to_dependents is True, dependents (parent_domain == domain_pattern)
    are kept but marked is_valid = False.
    """
    new_domain_sets = dict(profile.domain_sets)
    if domain_pattern in new_domain_sets:
        del new_domain_sets[domain_pattern]

    if cascade_to_dependents:
        for dom, dset in list(new_domain_sets.items()):
            if dset.parent_domain == domain_pattern and dset.is_valid:
                new_domain_sets[dom] = replace(dset, is_valid=False)

    return replace(profile, domain_sets=new_domain_sets)


def _is_mid_auth(page_url: str | None) -> bool:
    if not page_url:
        return False
    return any(pattern in page_url for pattern in MID_AUTH_URL_PATTERNS)


def _is_google_domain(domain: str) -> bool:
    return any(domain.endswith(gd) or domain == gd.lstrip(".") for gd in GOOGLE_DOMAINS)


def _cookie_remaining_ttl(cookie: BrowserCookie, now_sec: float) -> float:
    # If expires is -1 (session cookie) or missing, TTL is effectively 0 for refresh checks
    if cookie.expires <= 0:
        return 0.0
    return max(0.0, cookie.expires - now_sec)


def is_safe_google_update(
    existing_sid_ttl: float, incoming_sid_ttl: float, threshold_sec: float = SID_TTL_THRESHOLD_SEC
) -> bool:
    """Returns False if incoming TTL is more than threshold shorter than existing."""
    if incoming_sid_ttl < existing_sid_ttl - threshold_sec:
        return False
    return True


def merge_cookies(
    existing: ProfileIdentity, incoming: list[BrowserCookie], current_url: str | None = None
) -> ProfileIdentity:
    """TTL-aware merge of cookies into the profile."""
    import time

    now_sec = time.time()

    skip_google = _is_mid_auth(current_url)

    # Flatten all existing cookies by (name, domain, path)
    existing_cookies: dict[tuple[str, str, str], tuple[BrowserCookie, str]] = {}
    for dom, dset in existing.domain_sets.items():
        for cookie in dset.cookies:
            existing_cookies[(cookie.name, cookie.domain, cookie.path)] = (cookie, dom)

    incoming_cookies_by_domain: dict[str, dict[tuple[str, str, str], BrowserCookie]] = {}

    for cookie in incoming:
        is_google = _is_google_domain(cookie.domain)
        if is_google and skip_google:
            continue

        key = (cookie.name, cookie.domain, cookie.path)
        inc_ttl = _cookie_remaining_ttl(cookie, now_sec)

        # Determine the target domain_pattern (simple heuristic: use the cookie's domain)
        # Ideally, this should map back to the logical domain_pattern in domain_sets.
        # For simplicity, if it was already in a domain set, keep it there.
        # If not, map it to the cookie.domain.
        target_domain_pattern = cookie.domain

        if key in existing_cookies:
            exist_cookie, dom_pattern = existing_cookies[key]
            target_domain_pattern = dom_pattern
            exist_ttl = _cookie_remaining_ttl(exist_cookie, now_sec)

            if is_google and cookie.name in GOOGLE_SID_NAMES:
                if not is_safe_google_update(exist_ttl, inc_ttl, SID_TTL_THRESHOLD_SEC):
                    # Reject unsafe update, keep existing
                    if target_domain_pattern not in incoming_cookies_by_domain:
                        incoming_cookies_by_domain[target_domain_pattern] = {}
                    incoming_cookies_by_domain[target_domain_pattern][key] = exist_cookie
                    continue

            if exist_ttl > inc_ttl:
                # Keep existing if it has longer TTL
                if target_domain_pattern not in incoming_cookies_by_domain:
                    incoming_cookies_by_domain[target_domain_pattern] = {}
                incoming_cookies_by_domain[target_domain_pattern][key] = exist_cookie
                continue

        if target_domain_pattern not in incoming_cookies_by_domain:
            incoming_cookies_by_domain[target_domain_pattern] = {}
        incoming_cookies_by_domain[target_domain_pattern][key] = cookie

    # Reconstruct domain_sets
    new_domain_sets = dict(existing.domain_sets)

    # Fill with kept existing ones that weren't touched by incoming
    for key, (exist_cookie, dom_pattern) in existing_cookies.items():
        if (
            dom_pattern not in incoming_cookies_by_domain
            or key not in incoming_cookies_by_domain[dom_pattern]
        ):
            if dom_pattern not in incoming_cookies_by_domain:
                incoming_cookies_by_domain[dom_pattern] = {}
            incoming_cookies_by_domain[dom_pattern][key] = exist_cookie

    for dom_pattern, cookies_dict in incoming_cookies_by_domain.items():
        new_cookies = tuple(cookies_dict.values())
        if dom_pattern in new_domain_sets:
            dset = new_domain_sets[dom_pattern]
            new_domain_sets[dom_pattern] = replace(dset, cookies=new_cookies)
        else:
            # Create a new DomainCookieSet
            new_domain_sets[dom_pattern] = DomainCookieSet(
                domain_pattern=dom_pattern,
                cookies=new_cookies,
                local_storage={},
                health=CookieHealthUrgency.NONE,
                last_verified_at=None,
                is_valid=True,
            )

    return replace(existing, domain_sets=new_domain_sets)
