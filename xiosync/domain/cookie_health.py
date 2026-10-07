"""Cookie health classification and urgency computation.

4-tier Google cookie classification with urgency-based refresh scheduling.
Ported from XIOBR src/utils/cookie-health.mjs, adapted to XIOSYNC domain.

No I/O. No framework imports (RULE-ARCH-1; enforced by import-linter).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from xiosync.domain.profile_identity import (
    BrowserCookie,
    CookieExpiry,
    CookieHealthUrgency,
    CookieTier,
)

AUTH_REQUIRED = frozenset(
    {
        "__Secure-1PSID",
        "__Secure-3PSID",
        "SID",
        "SSID",
        "HSID",
        "APISID",
        "SAPISID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        "LSID",
    }
)

ROTATING = frozenset(
    {"__Secure-1PSIDTS", "__Secure-3PSIDTS", "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC", "AEC"}
)

LOGIN_TOKENS = frozenset({"__Secure-STRP"})

CRIT_DAYS = 7
WARN_DAYS = 30
MIN_COOKIE_COUNT = 20


@dataclass(frozen=True)
class CookieHealthReport:
    ok: bool
    missing: tuple[str, ...]
    expired: tuple[CookieExpiry, ...]
    expiring: tuple[CookieExpiry, ...]
    needs_refresh: bool
    refresh_urgency: CookieHealthUrgency
    total_cookies: int


def classify_cookie(name: str) -> CookieTier:
    """Classify a cookie name into one of the 4 tiers."""
    if name in AUTH_REQUIRED:
        return CookieTier.PRIMARY
    if name in ROTATING:
        return CookieTier.ROTATING
    if name in LOGIN_TOKENS:
        return CookieTier.LOGIN_EVENT
    return CookieTier.PREFERENCE


def check_cookie_health(
    cookies: list[BrowserCookie], now: float | None = None
) -> CookieHealthReport:
    """Full health check. Count cookies, find missing AUTH_REQUIRED names, check for expired/expiring."""
    if now is None:
        now = time.time()

    found_names = {c.name for c in cookies}
    missing = tuple(sorted(AUTH_REQUIRED - found_names))

    expired = []
    expiring = []

    crit_sec = CRIT_DAYS * 24 * 3600
    warn_sec = WARN_DAYS * 24 * 3600

    for c in cookies:
        if c.expires > 0:
            rem_sec = c.expires - now
            tier = classify_cookie(c.name)
            expiry = CookieExpiry(
                name=c.name,
                domain=c.domain,
                expires_at=c.expires,
                remaining_seconds=rem_sec,
                tier=tier,
            )
            if rem_sec <= 0:
                expired.append(expiry)
            elif rem_sec < warn_sec and tier in (
                CookieTier.PRIMARY,
                CookieTier.ROTATING,
                CookieTier.LOGIN_EVENT,
            ):
                expiring.append(expiry)

    expired = tuple(sorted(expired, key=lambda e: e.remaining_seconds))
    expiring = tuple(sorted(expiring, key=lambda e: e.remaining_seconds))

    total_cookies = len(cookies)

    ok = total_cookies >= MIN_COOKIE_COUNT and not missing and not expired

    # Compute urgency
    urgency = CookieHealthUrgency.NONE
    needs_refresh = False

    if missing or expired or total_cookies < MIN_COOKIE_COUNT:
        urgency = CookieHealthUrgency.IMMEDIATE
        needs_refresh = True
    elif expiring:
        # Check if any expiring is in CRIT_DAYS
        if any(e.remaining_seconds < crit_sec for e in expiring):
            urgency = CookieHealthUrgency.SOON
            needs_refresh = True
        else:
            urgency = CookieHealthUrgency.SCHEDULED
            needs_refresh = True

    return CookieHealthReport(
        ok=ok,
        missing=missing,
        expired=expired,
        expiring=expiring,
        needs_refresh=needs_refresh,
        refresh_urgency=urgency,
        total_cookies=total_cookies,
    )


def should_refresh(report: CookieHealthReport) -> bool:
    """Returns True if needs_refresh is True and urgency is SOON or IMMEDIATE."""
    return report.needs_refresh and report.refresh_urgency in (
        CookieHealthUrgency.SOON,
        CookieHealthUrgency.IMMEDIATE,
    )
