"""Unit tests for xiosync.domain.domain_registry (Universal Domain Registry)."""

from __future__ import annotations

from xiosync.domain.domain_registry import (
    DEFAULT_DOMAIN_MAP,
    auto_detect_services,
    classify_cookie_domain,
    get_auth_chain,
    get_dependent_domains,
    should_cascade_eviction,
)


class TestClassifyCookieDomain:
    def test_exact_google_match(self):
        result = classify_cookie_domain("accounts.google.com")
        assert result == "google.com"

    def test_suffix_google_match(self):
        result = classify_cookie_domain(".google.com")
        assert result == "google.com"

    def test_v0_dev_match(self):
        result = classify_cookie_domain("v0.dev")
        assert result == "v0.dev"

    def test_tailscale_match(self):
        result = classify_cookie_domain("login.tailscale.com")
        assert result == "tailscale.com"

    def test_github_match(self):
        result = classify_cookie_domain("github.com")
        assert result == "github.com"

    def test_unknown_domain_returns_none(self):
        result = classify_cookie_domain("randomsite.xyz")
        assert result is None


class TestGetDependentDomains:
    def test_google_has_dependents(self):
        deps = get_dependent_domains("google.com")
        assert "v0.dev" in deps
        assert "tailscale.com" in deps

    def test_github_has_dependents(self):
        deps = get_dependent_domains("github.com")
        assert "vercel.com" in deps

    def test_leaf_domain_has_no_dependents(self):
        deps = get_dependent_domains("v0.dev")
        assert deps == []

    def test_unknown_domain_has_no_dependents(self):
        deps = get_dependent_domains("unknown.xyz")
        assert deps == []


class TestGetAuthChain:
    def test_independent_domain_empty_chain(self):
        chain = get_auth_chain("google.com")
        assert chain == []

    def test_dependent_domain_has_parent(self):
        chain = get_auth_chain("v0.dev")
        assert "google.com" in chain

    def test_dependent_github_child(self):
        chain = get_auth_chain("vercel.com")
        assert "github.com" in chain


class TestShouldCascadeEviction:
    def test_evicting_google_cascades_to_v0(self):
        assert should_cascade_eviction("google.com", "v0.dev") is True

    def test_evicting_google_cascades_to_tailscale(self):
        assert should_cascade_eviction("google.com", "tailscale.com") is True

    def test_evicting_github_does_not_cascade_to_v0(self):
        assert should_cascade_eviction("github.com", "v0.dev") is False

    def test_evicting_google_does_not_cascade_to_github(self):
        assert should_cascade_eviction("google.com", "github.com") is False


class TestAutoDetectServices:
    def test_groups_cookies_by_service(self):
        cookies = [
            {"domain": ".google.com", "name": "SID"},
            {"domain": "accounts.google.com", "name": "HSID"},
            {"domain": "v0.dev", "name": "session"},
            {"domain": ".unknown-site.org", "name": "tracker"},
        ]
        result = auto_detect_services(cookies)
        assert "google.com" in result
        assert len(result["google.com"]) == 2
        assert "v0.dev" in result
        assert len(result["v0.dev"]) == 1

    def test_empty_cookies(self):
        result = auto_detect_services([])
        assert result == {}


class TestDefaultDomainMap:
    def test_has_google(self):
        assert "google.com" in DEFAULT_DOMAIN_MAP
        assert DEFAULT_DOMAIN_MAP["google.com"].auth_method == "direct"
        assert DEFAULT_DOMAIN_MAP["google.com"].parent_domain is None

    def test_v0_depends_on_google(self):
        assert "v0.dev" in DEFAULT_DOMAIN_MAP
        assert DEFAULT_DOMAIN_MAP["v0.dev"].parent_domain == "google.com"
        assert DEFAULT_DOMAIN_MAP["v0.dev"].auth_method == "google_oauth"

    def test_github_is_independent(self):
        assert "github.com" in DEFAULT_DOMAIN_MAP
        assert DEFAULT_DOMAIN_MAP["github.com"].parent_domain is None

    def test_has_at_least_6_entries(self):
        assert len(DEFAULT_DOMAIN_MAP) >= 6
