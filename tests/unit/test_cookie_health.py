"""Unit tests for xiosync.domain.cookie_health (4-tier classification)."""
from __future__ import annotations

import time

import pytest

from xiosync.domain.profile_identity import BrowserCookie, CookieTier, CookieHealthUrgency
from xiosync.domain.cookie_health import (
    AUTH_REQUIRED,
    ROTATING,
    LOGIN_TOKENS,
    CRIT_DAYS,
    WARN_DAYS,
    MIN_COOKIE_COUNT,
    classify_cookie,
    check_cookie_health,
    should_refresh,
)


def _cookie(name: str, domain: str = ".google.com", expires: float = -1) -> BrowserCookie:
    return BrowserCookie(
        name=name, value="v", domain=domain, path="/",
        expires=expires, httpOnly=True, secure=True, sameSite="Lax", size=None,
    )


# ── Tests: Cookie classification ─────────────────────────────────────

class TestClassifyCookie:
    def test_auth_required_cookies(self):
        for name in ("SID", "SSID", "HSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"):
            assert classify_cookie(name) == CookieTier.PRIMARY, f"{name} should be PRIMARY"

    def test_rotating_cookies(self):
        for name in ("SIDCC", "__Secure-1PSIDCC", "AEC", "__Secure-1PSIDTS"):
            assert classify_cookie(name) == CookieTier.ROTATING, f"{name} should be ROTATING"

    def test_login_token_cookies(self):
        assert classify_cookie("__Secure-STRP") == CookieTier.LOGIN_EVENT

    def test_unknown_is_preference(self):
        assert classify_cookie("random_tracking_cookie") == CookieTier.PREFERENCE
        assert classify_cookie("NID") == CookieTier.PREFERENCE


# ── Tests: Health check ──────────────────────────────────────────────

class TestCheckCookieHealth:
    def test_healthy_session_with_all_cookies(self):
        now = time.time()
        cookies = []
        # Add all AUTH_REQUIRED cookies with long TTL
        for name in AUTH_REQUIRED:
            cookies.append(_cookie(name, expires=now + 90 * 86400))  # 90 days
        # Add rotating cookies
        for name in ROTATING:
            cookies.append(_cookie(name, expires=now + 60 * 86400))
        # Pad to MIN_COOKIE_COUNT
        for i in range(max(0, MIN_COOKIE_COUNT - len(cookies))):
            cookies.append(_cookie(f"pref_{i}", expires=now + 365 * 86400))

        report = check_cookie_health(cookies, now=now)
        assert report.ok is True
        assert len(report.missing) == 0
        assert report.refresh_urgency == CookieHealthUrgency.NONE

    def test_missing_auth_cookie_flags_not_ok(self):
        now = time.time()
        # Only add some AUTH_REQUIRED, skip SID
        cookies = [_cookie(name, expires=now + 90 * 86400) for name in list(AUTH_REQUIRED)[1:]]
        for i in range(MIN_COOKIE_COUNT):
            cookies.append(_cookie(f"pad_{i}", expires=now + 365 * 86400))

        report = check_cookie_health(cookies, now=now)
        assert report.ok is False
        assert len(report.missing) > 0

    def test_expiring_within_crit_days_triggers_immediate(self):
        now = time.time()
        cookies = []
        for name in AUTH_REQUIRED:
            # Auth cookies expiring in 3 days (< CRIT_DAYS=7)
            cookies.append(_cookie(name, expires=now + 3 * 86400))
        for name in ROTATING:
            cookies.append(_cookie(name, expires=now + 3 * 86400))
        for i in range(MIN_COOKIE_COUNT):
            cookies.append(_cookie(f"pad_{i}", expires=now + 365 * 86400))

        report = check_cookie_health(cookies, now=now)
        assert report.needs_refresh is True
        assert report.refresh_urgency in (CookieHealthUrgency.IMMEDIATE, CookieHealthUrgency.SOON)

    def test_too_few_cookies_flags_needs_refresh(self):
        now = time.time()
        # Only 5 cookies total (< MIN_COOKIE_COUNT=20)
        cookies = [_cookie(f"c_{i}", expires=now + 90 * 86400) for i in range(5)]
        report = check_cookie_health(cookies, now=now)
        assert report.total_cookies == 5
        assert report.needs_refresh is True

    def test_empty_cookies_list(self):
        report = check_cookie_health([])
        assert report.ok is False
        assert report.total_cookies == 0
        assert report.needs_refresh is True


# ── Tests: should_refresh ────────────────────────────────────────────

class TestShouldRefresh:
    def test_returns_true_for_immediate(self):
        report = check_cookie_health([])  # Empty = immediate
        assert should_refresh(report) is True
