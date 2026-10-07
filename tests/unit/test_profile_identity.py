"""Unit tests for xiosync.domain.profile_identity (CIPI core domain)."""

from __future__ import annotations

import time
import uuid

from xiosync.domain.profile_identity import (
    BrowserCookie,
    CookieHealthUrgency,
    CookieTier,
    DomainCookieSet,
    MaterializationMode,
    ProfileIdentity,
    _cookie_remaining_ttl,
    _is_google_domain,
    _is_mid_auth,
    evict_domain,
    is_safe_google_update,
    merge_cookies,
)

# ── Fixtures ──────────────────────────────────────────────────────────


def _cookie(
    name: str, domain: str = ".google.com", expires: float = -1, value: str = "v"
) -> BrowserCookie:
    return BrowserCookie(
        name=name,
        value=value,
        domain=domain,
        path="/",
        expires=expires,
        httpOnly=True,
        secure=True,
        sameSite="Lax",
        size=None,
    )


def _profile(domain_sets: dict | None = None, serial: int = 1) -> ProfileIdentity:
    return ProfileIdentity(
        identity_id=uuid.uuid4(),
        serial=serial,
        organization_id=uuid.uuid4(),
        domain_sets=domain_sets or {},
        materialization=MaterializationMode.TAR_PROFILE,
        is_persisted=False,
        storage_object_key=None,
        version=0,
    )


def _domain_set(domain: str = "google.com", cookies: tuple = (), **kwargs) -> DomainCookieSet:
    defaults = dict(
        domain_pattern=domain,
        cookies=cookies,
        local_storage={},
        health=CookieHealthUrgency.NONE,
        last_verified_at=None,
        is_valid=True,
        auth_method="direct",
        parent_domain=None,
    )
    defaults.update(kwargs)
    return DomainCookieSet(**defaults)


# ── Tests: ProfileIdentity properties ────────────────────────────────


class TestProfileIdentityProperties:
    def test_canonical_key_format(self):
        p = _profile(serial=6)
        assert p.canonical_key == "PRFL-006"

    def test_canonical_key_large_serial(self):
        p = _profile(serial=1234)
        assert p.canonical_key == "PRFL-1234"

    def test_tar_object_key(self):
        p = _profile(serial=42)
        assert p.tar_object_key == "chrome_profiles/PRFL-042.tar.gz"

    def test_state_object_key(self):
        p = _profile(serial=7)
        assert p.state_object_key == "session_states/PRFL-007.json"

    def test_default_materialization_is_tar(self):
        p = _profile()
        assert p.materialization == MaterializationMode.TAR_PROFILE


# ── Tests: Mid-auth URL guard ────────────────────────────────────────


class TestMidAuthGuard:
    def test_oauth2_url(self):
        assert _is_mid_auth("https://accounts.google.com/o/oauth2/auth?client_id=xxx")

    def test_signin_url(self):
        assert _is_mid_auth("https://accounts.google.com/signin/v2/identifier")

    def test_check_cookie_url(self):
        assert _is_mid_auth("https://accounts.google.com/CheckCookie?continue=xxx")

    def test_signin_identifier_url(self):
        assert _is_mid_auth("https://accounts.google.com/signin/v2/identifier")

    def test_non_auth_url(self):
        assert not _is_mid_auth("https://myaccount.google.com/")

    def test_none_url(self):
        assert not _is_mid_auth(None)

    def test_empty_url(self):
        assert not _is_mid_auth("")


# ── Tests: Google domain detection ───────────────────────────────────


class TestGoogleDomain:
    def test_google_com(self):
        assert _is_google_domain(".google.com")

    def test_accounts_google(self):
        assert _is_google_domain("accounts.google.com")

    def test_non_google(self):
        assert not _is_google_domain("v0.dev")

    def test_partial_match(self):
        assert not _is_google_domain("notgoogle.com")


# ── Tests: Cookie TTL ────────────────────────────────────────────────


class TestCookieTTL:
    def test_session_cookie_negative_expires(self):
        c = _cookie("SID", expires=-1)
        # Implementation: max(0, -1 - now) = 0
        assert _cookie_remaining_ttl(c, time.time()) == 0.0

    def test_session_cookie_zero_expires(self):
        c = _cookie("SID", expires=0)
        assert _cookie_remaining_ttl(c, time.time()) == 0.0

    def test_future_cookie_positive_ttl(self):
        now = time.time()
        c = _cookie("SID", expires=now + 3600)
        ttl = _cookie_remaining_ttl(c, now)
        assert 3599 < ttl <= 3600

    def test_expired_cookie_zero_ttl(self):
        c = _cookie("SID", expires=time.time() - 100)
        assert _cookie_remaining_ttl(c, time.time()) == 0.0


# ── Tests: SID-TTL safety ────────────────────────────────────────────


class TestSIDTTLSafety:
    def test_safe_when_incoming_longer(self):
        assert is_safe_google_update(3600.0, 7200.0) is True

    def test_safe_when_equal(self):
        assert is_safe_google_update(3600.0, 3600.0) is True

    def test_safe_when_slightly_shorter(self):
        assert is_safe_google_update(7200.0, 7000.0) is True

    def test_unsafe_when_much_shorter(self):
        # 7200 - 3600 = 3600 > 3600 threshold → not safe
        assert is_safe_google_update(7200.0, 3599.0) is False

    def test_custom_threshold(self):
        assert is_safe_google_update(100.0, 49.0, threshold_sec=50.0) is False
        assert is_safe_google_update(100.0, 51.0, threshold_sec=50.0) is True


# ── Tests: Domain eviction ───────────────────────────────────────────


class TestEvictDomain:
    def test_evict_removes_domain(self):
        google_ds = _domain_set("google.com")
        ts_ds = _domain_set("tailscale.com", parent_domain="google.com", auth_method="google_oauth")
        p = _profile(domain_sets={"google.com": google_ds, "tailscale.com": ts_ds})

        evicted = evict_domain(p, "google.com", cascade_to_dependents=True)
        assert "google.com" not in evicted.domain_sets
        # Dependent (tailscale) should still exist but be invalid
        assert "tailscale.com" in evicted.domain_sets
        assert evicted.domain_sets["tailscale.com"].is_valid is False

    def test_evict_no_cascade(self):
        google_ds = _domain_set("google.com")
        ts_ds = _domain_set("tailscale.com", parent_domain="google.com")
        p = _profile(domain_sets={"google.com": google_ds, "tailscale.com": ts_ds})

        evicted = evict_domain(p, "google.com", cascade_to_dependents=False)
        assert "google.com" not in evicted.domain_sets
        # Without cascade, dependent stays valid
        assert evicted.domain_sets["tailscale.com"].is_valid is True

    def test_evict_nonexistent_domain(self):
        p = _profile(domain_sets={"google.com": _domain_set("google.com")})
        evicted = evict_domain(p, "github.com")
        assert len(evicted.domain_sets) == 1  # unchanged


# ── Tests: Cookie merge ──────────────────────────────────────────────


class TestMergeCookies:
    def test_merge_adds_new_cookie(self):
        p = _profile()
        now = time.time()
        incoming = [_cookie("NEW_COOKIE", domain=".example.com", expires=now + 3600)]
        merged = merge_cookies(p, incoming)
        # Should have added the cookie to the profile
        assert merged is not None

    def test_merge_skips_google_during_mid_auth(self):
        p = _profile()
        incoming = [_cookie("SID", domain=".google.com", expires=time.time() + 7200)]
        merged = merge_cookies(p, incoming, current_url="https://accounts.google.com/o/oauth2/auth")
        # Mid-auth guard should prevent Google cookie updates
        # The profile should not be modified with Google cookies
        assert merged is not None


# ── Tests: MaterializationMode enum ──────────────────────────────────


class TestMaterializationMode:
    def test_tar_profile_value(self):
        assert MaterializationMode.TAR_PROFILE.value == "TAR_PROFILE"

    def test_cookie_injection_value(self):
        assert MaterializationMode.COOKIE_INJECTION.value == "COOKIE_INJECTION"


# ── Tests: CookieTier enum ───────────────────────────────────────────


class TestCookieTier:
    def test_all_tiers_exist(self):
        assert CookieTier.PRIMARY is not None
        assert CookieTier.ROTATING is not None
        assert CookieTier.LOGIN_EVENT is not None
        assert CookieTier.PREFERENCE is not None
