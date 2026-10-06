"""Unit tests for PAC generator, HITL, domain eviction, ontology, and TOTP."""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

# ── PAC Generator ────────────────────────────────────────────────────

from xiosync.subsystems.xiogrid.pac_generator import (
    DomainProxyRule,
    PACConfig,
    generate_pac_file,
    write_pac_for_session,
    cleanup_pac_file,
    build_chrome_proxy_args,
)


class TestPACGenerator:
    def test_simple_pac_with_default_only(self):
        config = PACConfig(profile_default_proxy="SOCKS5 vm:10017")
        pac = generate_pac_file(config)
        assert "FindProxyForURL" in pac
        assert "SOCKS5 vm:10017" in pac

    def test_pac_with_domain_rules(self):
        config = PACConfig(
            profile_default_proxy="SOCKS5 vm:10017",
            domain_rules=[
                DomainProxyRule(domain_pattern="v0.dev", proxy_url="SOCKS5 vm:10042", priority=10),
                DomainProxyRule(domain_pattern="github.com", proxy_url="SOCKS5 vm:10099", priority=5),
            ],
        )
        pac = generate_pac_file(config)
        assert "v0.dev" in pac
        assert "github.com" in pac
        assert "SOCKS5 vm:10042" in pac
        assert "SOCKS5 vm:10099" in pac
        # v0.dev (priority 10) should appear before github.com (priority 5)
        assert pac.index("v0.dev") < pac.index("github.com")

    def test_workflow_override_replaces_everything(self):
        config = PACConfig(
            profile_default_proxy="SOCKS5 vm:10017",
            domain_rules=[
                DomainProxyRule(domain_pattern="v0.dev", proxy_url="SOCKS5 vm:10042"),
            ],
            workflow_override="SOCKS5 vm:10099",
        )
        pac = generate_pac_file(config)
        assert "SOCKS5 vm:10099" in pac
        # Domain rules should NOT appear
        assert "v0.dev" not in pac

    def test_write_and_cleanup_pac_file(self):
        config = PACConfig(profile_default_proxy="SOCKS5 vm:10017")
        pac_url = write_pac_for_session("test-session-123", config)
        assert pac_url.startswith("file://")
        pac_path = pac_url.replace("file://", "")
        assert os.path.exists(pac_path)
        # Cleanup
        assert cleanup_pac_file("test-session-123") is True
        assert not os.path.exists(pac_path)

    def test_cleanup_nonexistent_returns_false(self):
        assert cleanup_pac_file("nonexistent-session") is False

    def test_build_chrome_proxy_args_pac(self):
        args = build_chrome_proxy_args(pac_url="file:///tmp/test.pac")
        assert any("--proxy-pac-url" in a for a in args)

    def test_build_chrome_proxy_args_proxy(self):
        args = build_chrome_proxy_args(proxy_url="socks5://vm:10017")
        assert any("--proxy-server" in a for a in args)

    def test_build_chrome_proxy_args_empty(self):
        args = build_chrome_proxy_args()
        assert args == []


# ── HITL ─────────────────────────────────────────────────────────────

from xiosync.subsystems.xiorun.hitl import (
    HITLNotice,
    HITLNoticeStore,
    HITLResumedBy,
    HITLState,
)


class TestHITLNoticeStore:
    def test_create_and_get(self):
        store = HITLNoticeStore()
        notice = HITLNotice(
            organization_id=uuid.uuid4(),
            session_id="sess-1",
            challenge_type="totp_rejected",
            message="TOTP was rejected",
        )
        created = store.create(notice)
        assert created.state == HITLState.PENDING
        assert store.get(created.id) is not None

    def test_list_pending(self):
        store = HITLNoticeStore()
        org = uuid.uuid4()
        for i in range(3):
            store.create(HITLNotice(
                organization_id=org, session_id=f"sess-{i}",
                challenge_type="captcha", message=f"Captcha {i}",
            ))
        pending = store.list_pending(org)
        assert len(pending) == 3

    def test_resume_changes_state(self):
        store = HITLNoticeStore()
        notice = store.create(HITLNotice(
            organization_id=uuid.uuid4(), session_id="s1",
            challenge_type="2fa", message="Need 2FA",
        ))
        resumed = store.resume(notice.id, HITLResumedBy.AI_AGENT)
        assert resumed.state == HITLState.RESUMED
        assert resumed.resumed_by == HITLResumedBy.AI_AGENT
        assert resumed.resumed_at is not None

    def test_cancel_changes_state(self):
        store = HITLNoticeStore()
        notice = store.create(HITLNotice(
            organization_id=uuid.uuid4(), session_id="s1",
            challenge_type="2fa", message="Need 2FA",
        ))
        cancelled = store.cancel(notice.id)
        assert cancelled.state == HITLState.CANCELLED

    def test_wait_for_resume_unblocks(self):
        store = HITLNoticeStore()
        notice = store.create(HITLNotice(
            organization_id=uuid.uuid4(), session_id="s1",
            challenge_type="2fa", message="test",
        ))

        async def _test():
            async def _resume_after_delay():
                await asyncio.sleep(0.1)
                store.resume(notice.id)

            asyncio.create_task(_resume_after_delay())
            result = await store.wait_for_resume(notice.id, timeout=5.0)
            assert result.state == HITLState.RESUMED

        asyncio.run(_test())


# ── Domain Eviction ──────────────────────────────────────────────────

from xiosync.subsystems.xiorun.domain_eviction import (
    _domain_matches_cookie,
    evict_domain_from_state,
)


class TestDomainEviction:
    def test_domain_matches_cookie_exact(self):
        assert _domain_matches_cookie(".google.com", "google.com") is True

    def test_domain_matches_cookie_subdomain(self):
        assert _domain_matches_cookie("accounts.google.com", "google.com") is True

    def test_domain_matches_cookie_no_match(self):
        assert _domain_matches_cookie("v0.dev", "google.com") is False

    def test_evict_domain_from_state_removes_cookies(self):
        state = {
            "cookies": [
                {"name": "SID", "domain": ".google.com"},
                {"name": "HSID", "domain": "accounts.google.com"},
                {"name": "session", "domain": "v0.dev"},
            ],
        }
        result = evict_domain_from_state(state, "google.com", cascade_to_dependents=False)
        remaining = result.get("cookies", [])
        # Only v0.dev cookie should remain
        google_cookies = [c for c in remaining if "google" in c.get("domain", "")]
        assert len(google_cookies) == 0
        v0_cookies = [c for c in remaining if "v0" in c.get("domain", "")]
        assert len(v0_cookies) == 1

    def test_evict_preserves_unrelated_domains(self):
        state = {
            "cookies": [
                {"name": "gh_session", "domain": "github.com"},
                {"name": "SID", "domain": ".google.com"},
            ],
        }
        result = evict_domain_from_state(state, "google.com")
        remaining = result.get("cookies", [])
        gh_cookies = [c for c in remaining if "github" in c.get("domain", "")]
        assert len(gh_cookies) == 1


# ── Ontology Edge Types ──────────────────────────────────────────────

from xiosync.domain.ontology import (
    EDGE_TYPE_AUTHENTICATES_AS,
    EDGE_TYPE_BINDS_NETWORK,
    EDGE_TYPE_DEPENDS_ON_AUTH,
    EDGE_TYPE_MATERIALIZES_TO,
    EDGE_TYPE_ROUTES_THROUGH,
    EDGE_TYPE_RUNS_ON,
    WELL_KNOWN_EDGE_TYPES,
    GRAPH_CLASSES,
    validate_graph_class,
    would_create_cycle,
)


class TestOntologyEdgeTypes:
    def test_all_edge_types_in_well_known_set(self):
        assert EDGE_TYPE_AUTHENTICATES_AS in WELL_KNOWN_EDGE_TYPES
        assert EDGE_TYPE_MATERIALIZES_TO in WELL_KNOWN_EDGE_TYPES
        assert EDGE_TYPE_ROUTES_THROUGH in WELL_KNOWN_EDGE_TYPES
        assert EDGE_TYPE_RUNS_ON in WELL_KNOWN_EDGE_TYPES
        assert EDGE_TYPE_DEPENDS_ON_AUTH in WELL_KNOWN_EDGE_TYPES
        assert EDGE_TYPE_BINDS_NETWORK in WELL_KNOWN_EDGE_TYPES

    def test_well_known_has_6_entries(self):
        assert len(WELL_KNOWN_EDGE_TYPES) == 6

    def test_graph_classes_unchanged(self):
        assert len(GRAPH_CLASSES) == 4

    def test_existing_cycle_detection_still_works(self):
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        adj = {a: {b}, b: {c}}
        assert would_create_cycle(source_id=c, target_id=a, adjacency=adj) is True
        assert would_create_cycle(source_id=a, target_id=c, adjacency=adj) is False


# ── Session Verifier ─────────────────────────────────────────────────

from xiosync.subsystems.xiorun.session_verifier import extract_email_prefix


class TestSessionVerifier:
    def test_extract_email_prefix(self):
        assert extract_email_prefix("user@gmail.com") == "user"

    def test_extract_email_prefix_complex(self):
        assert extract_email_prefix("First.Last+tag@company.org") == "first.last+tag"


# ── Google Token Verify ──────────────────────────────────────────────

from xiosync.subsystems.xiorun.google_token_verify import detect_runtime_type, RuntimeIdentity


class TestGoogleTokenVerify:
    def test_detect_runtime_type_returns_string(self):
        rt = detect_runtime_type()
        assert isinstance(rt, str)
        assert rt in ("colab_gpu", "colab_cpu", "docker", "bare_metal", "vm")

    def test_runtime_identity_dataclass(self):
        ri = RuntimeIdentity(email=None, runtime_type="vm", verified=False)
        assert ri.verified is False
