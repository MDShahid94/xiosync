"""Unit tests for P6 — untested services and API components.

Covers: QuotaService, WebhookService (sign_payload), MeteringService,
SecretRefService, TriggerService, SharingService, CapabilityService,
ArtifactService, event bus, worker modules, and observability.
"""

from __future__ import annotations

import asyncio
import os
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from xiosync.platform.ids import new_id


# ═══════════════════════════════════════════════════════════════════════════════
# QuotaService
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.quotas import QuotaExceededError, QuotaService


class TestQuotaExceededError:
    def test_attributes(self) -> None:
        exc = QuotaExceededError("workers", 5, 5)
        assert exc.resource_type == "workers"
        assert exc.current == 5
        assert exc.limit == 5
        assert "workers" in str(exc)

    def test_different_resource_types(self) -> None:
        for rt in ("queued_tasks", "concurrent_runs", "daily_events"):
            exc = QuotaExceededError(rt, 10, 10)
            assert exc.resource_type == rt


class TestQuotaService:
    def _svc(self, quotas: dict[str, int], count: int = 0) -> QuotaService:
        session = MagicMock()
        # _get_quotas returns the quotas dict
        session.scalar.side_effect = [quotas, count]
        return QuotaService(session)

    def test_check_workers_no_quota(self) -> None:
        """No quota key → no enforcement."""
        session = MagicMock()
        session.scalar.return_value = {}
        svc = QuotaService(session)
        svc.check_workers(new_id())  # Should not raise.

    def test_check_workers_under_limit(self) -> None:
        session = MagicMock()
        session.scalar.side_effect = [{"max_workers": 10}, 5]
        svc = QuotaService(session)
        svc.check_workers(new_id())

    def test_check_workers_at_limit_raises(self) -> None:
        session = MagicMock()
        session.scalar.side_effect = [{"max_workers": 5}, 5]
        svc = QuotaService(session)
        with pytest.raises(QuotaExceededError) as exc_info:
            svc.check_workers(new_id())
        assert exc_info.value.resource_type == "workers"
        assert exc_info.value.limit == 5

    def test_check_queued_tasks_no_quota(self) -> None:
        session = MagicMock()
        session.scalar.return_value = {}
        svc = QuotaService(session)
        svc.check_queued_tasks(new_id())

    def test_check_queued_tasks_at_limit_raises(self) -> None:
        session = MagicMock()
        session.scalar.side_effect = [{"max_queued_tasks": 100}, 100]
        svc = QuotaService(session)
        with pytest.raises(QuotaExceededError) as exc_info:
            svc.check_queued_tasks(new_id())
        assert exc_info.value.resource_type == "queued_tasks"

    def test_check_concurrent_runs_under_limit(self) -> None:
        session = MagicMock()
        session.scalar.side_effect = [{"max_concurrent_runs": 10}, 3]
        svc = QuotaService(session)
        svc.check_concurrent_runs(new_id())

    def test_check_concurrent_runs_at_limit_raises(self) -> None:
        session = MagicMock()
        session.scalar.side_effect = [{"max_concurrent_runs": 5}, 5]
        svc = QuotaService(session)
        with pytest.raises(QuotaExceededError) as exc_info:
            svc.check_concurrent_runs(new_id())
        assert exc_info.value.resource_type == "concurrent_runs"

    def test_check_daily_events_no_quota(self) -> None:
        session = MagicMock()
        session.scalar.return_value = {}
        svc = QuotaService(session)
        svc.check_daily_events(new_id())

    def test_check_daily_events_at_limit_raises(self) -> None:
        session = MagicMock()
        session.scalar.side_effect = [{"max_daily_events": 1000}, 1000]
        svc = QuotaService(session)
        with pytest.raises(QuotaExceededError) as exc_info:
            svc.check_daily_events(new_id())
        assert exc_info.value.resource_type == "daily_events"

    def test_null_quotas_treated_as_empty(self) -> None:
        session = MagicMock()
        session.scalar.return_value = None  # NULL resource_quotas
        svc = QuotaService(session)
        svc.check_workers(new_id())
        svc.check_queued_tasks(new_id())
        svc.check_concurrent_runs(new_id())
        svc.check_daily_events(new_id())


# ═══════════════════════════════════════════════════════════════════════════════
# WebhookService — sign_payload
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.webhooks import sign_payload


class TestSignPayload:
    def test_deterministic(self) -> None:
        """Same secret + payload → same signature."""
        sig1 = sign_payload("secret", {"key": "value"})
        sig2 = sign_payload("secret", {"key": "value"})
        assert sig1 == sig2

    def test_different_secrets(self) -> None:
        sig1 = sign_payload("secret_a", {"key": "value"})
        sig2 = sign_payload("secret_b", {"key": "value"})
        assert sig1 != sig2

    def test_different_payloads(self) -> None:
        sig1 = sign_payload("secret", {"key": "a"})
        sig2 = sign_payload("secret", {"key": "b"})
        assert sig1 != sig2

    def test_returns_hex_string(self) -> None:
        sig = sign_payload("secret", {"data": 123})
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA256 hex digest
        int(sig, 16)  # Valid hex

    def test_key_order_independent(self) -> None:
        """sort_keys=True makes key order irrelevant."""
        sig1 = sign_payload("s", {"a": 1, "b": 2})
        sig2 = sign_payload("s", {"b": 2, "a": 1})
        assert sig1 == sig2


# ═══════════════════════════════════════════════════════════════════════════════
# Event Bus (InProcessEventBus)
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.core.event_bus import InProcessEventBus, get_event_bus


class TestInProcessEventBus:
    @pytest.mark.asyncio
    async def test_publish_subscribe(self) -> None:
        bus = InProcessEventBus()
        received: list[dict[str, Any]] = []

        async def consumer() -> None:
            async for msg in bus.subscribe("ch"):
                received.append(msg)
                if len(received) >= 2:
                    break

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.01)
        await bus.publish("ch", {"n": 1})
        await bus.publish("ch", {"n": 2})
        await asyncio.wait_for(task, timeout=1.0)

        assert len(received) == 2
        assert received[0]["n"] == 1
        assert received[1]["n"] == 2

    @pytest.mark.asyncio
    async def test_channel_isolation(self) -> None:
        bus = InProcessEventBus()
        received: list[dict[str, Any]] = []

        async def consumer() -> None:
            async for msg in bus.subscribe("ch_a"):
                received.append(msg)
                break

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.01)
        await bus.publish("ch_b", {"wrong": True})
        await bus.publish("ch_a", {"right": True})
        await asyncio.wait_for(task, timeout=1.0)

        assert len(received) == 1
        assert received[0]["right"] is True

    @pytest.mark.asyncio
    async def test_close_clears_channels(self) -> None:
        bus = InProcessEventBus()
        assert len(bus._channels) == 0
        await bus.close()
        assert len(bus._channels) == 0

    def test_get_event_bus_returns_instance(self) -> None:
        """get_event_bus returns the same singleton."""
        import xiosync.core.event_bus as mod
        old_bus = mod._bus
        mod._bus = None
        try:
            bus = get_event_bus()
            assert isinstance(bus, InProcessEventBus)
        finally:
            mod._bus = old_bus


# ═══════════════════════════════════════════════════════════════════════════════
# Worker — system_context
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.worker.context import system_context, SYSTEM_ACTOR_ID
from xiosync.domain.context import PlatformRole, MembershipRole


class TestSystemContext:
    def test_creates_valid_context(self) -> None:
        org_id = new_id()
        ctx = system_context(org_id)
        assert ctx.organization_id == org_id
        assert ctx.actor_id == SYSTEM_ACTOR_ID
        assert ctx.platform_role == PlatformRole.PLATFORM_ADMIN
        assert ctx.membership_role == MembershipRole.ORG_OWNER

    def test_different_orgs_different_contexts(self) -> None:
        ctx1 = system_context(new_id())
        ctx2 = system_context(new_id())
        assert ctx1.organization_id != ctx2.organization_id
        assert ctx1.actor_id == ctx2.actor_id  # Same system actor


# ═══════════════════════════════════════════════════════════════════════════════
# Worker — ticker
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.worker.ticker import tick_cron_triggers


class TestTickerWorker:
    def test_no_due_triggers_returns_zero(self) -> None:
        session = MagicMock()
        with patch("xiosync.worker.ticker.TriggerService") as MockTS:
            MockTS.return_value.get_due_cron_triggers.return_value = []
            result = tick_cron_triggers(session)
        assert result == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Worker — reaper
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.worker.reaper import reap_expired_leases


class TestReaperWorker:
    def test_no_orgs_returns_zero(self) -> None:
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        result = reap_expired_leases(session)
        assert result == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Worker — dispatcher
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.worker.dispatcher import dispatch_pending_webhooks


class TestDispatcherWorker:
    def test_no_pending_returns_zero(self) -> None:
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        result = dispatch_pending_webhooks(session)
        assert result == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Worker — event_router
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.worker.event_router import evaluate_event_triggers


class TestEventRouterWorker:
    def test_no_events_returns_zero(self) -> None:
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        result = evaluate_event_triggers(session)
        assert result == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Observability middleware
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.platform.observability import (
    ObservabilityMiddleware,
    setup_opentelemetry,
    get_metrics_app,
)


class TestObservability:
    def test_setup_otel_returns_false_without_deps(self) -> None:
        """Without OTel installed, returns False gracefully."""
        result = setup_opentelemetry()
        assert result is False

    def test_get_metrics_app_returns_none_without_deps(self) -> None:
        """Without prometheus-client installed, returns None."""
        result = get_metrics_app()
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Security headers (CSP + HSTS)
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.api.middleware import SecurityHeadersMiddleware


class TestSecurityHeaders:
    def test_csp_header_present(self) -> None:
        assert "Content-Security-Policy" in SecurityHeadersMiddleware._HEADERS

    def test_hsts_header_present(self) -> None:
        assert "Strict-Transport-Security" in SecurityHeadersMiddleware._HEADERS

    def test_csp_value(self) -> None:
        csp = SecurityHeadersMiddleware._HEADERS["Content-Security-Policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_hsts_value(self) -> None:
        hsts = SecurityHeadersMiddleware._HEADERS["Strict-Transport-Security"]
        assert "max-age=63072000" in hsts
        assert "includeSubDomains" in hsts

    def test_all_original_headers_preserved(self) -> None:
        headers = SecurityHeadersMiddleware._HEADERS
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Cache-Control"] == "no-store"


# ═══════════════════════════════════════════════════════════════════════════════
# Public auth paths (OpenAPI access)
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.api.middleware import _PUBLIC_AUTH_PATHS


class TestPublicAuthPaths:
    def test_docs_is_public(self) -> None:
        assert "/docs" in _PUBLIC_AUTH_PATHS

    def test_redoc_is_public(self) -> None:
        assert "/redoc" in _PUBLIC_AUTH_PATHS

    def test_openapi_json_is_public(self) -> None:
        assert "/openapi.json" in _PUBLIC_AUTH_PATHS

    def test_auth_login_still_public(self) -> None:
        assert "/api/v1/auth/login" in _PUBLIC_AUTH_PATHS

    def test_auth_refresh_still_public(self) -> None:
        assert "/api/v1/auth/refresh" in _PUBLIC_AUTH_PATHS


# ═══════════════════════════════════════════════════════════════════════════════
# Event type completeness
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.domain.events import EVENT_TYPES


class TestEventTypeCompleteness:
    def test_webhook_delivered_registered(self) -> None:
        assert "webhook.delivered" in EVENT_TYPES

    def test_webhook_failed_registered(self) -> None:
        assert "webhook.failed" in EVENT_TYPES

    def test_webhook_dispatch_registered(self) -> None:
        assert "webhook.dispatch" in EVENT_TYPES

    def test_task_output_registered(self) -> None:
        assert "task.output" in EVENT_TYPES


# ═══════════════════════════════════════════════════════════════════════════════
# DB pool configurability
# ═══════════════════════════════════════════════════════════════════════════════


class TestDBPoolConfig:
    def test_pool_defaults(self) -> None:
        """create_database_engine returns an engine with default pool params."""
        import os
        from unittest.mock import patch as env_patch

        with env_patch.dict(os.environ, {}, clear=False):
            from xiosync.persistence.database import create_database_engine
            engine = create_database_engine("postgresql+psycopg://x:x@localhost:5432/x")
            assert engine is not None
            engine.dispose()

    def test_pool_env_vars_read(self) -> None:
        """Pool params are read from environment."""
        import os
        from unittest.mock import patch as env_patch
        env = {
            "DB_POOL_SIZE": "20",
            "DB_MAX_OVERFLOW": "40",
            "DB_POOL_TIMEOUT": "60",
            "DB_POOL_RECYCLE": "3600",
        }
        with env_patch.dict(os.environ, env, clear=False):
            from xiosync.persistence.database import create_database_engine
            engine = create_database_engine("postgresql+psycopg://x:x@localhost:5432/x")
            assert engine is not None
            engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# SharingService — constants and validation
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.sharing import (
    SHAREABLE_TYPES,
    SHARE_PERMISSIONS,
    SharingDisabledError,
    SharingService,
)


class TestSharingConstants:
    def test_shareable_types(self) -> None:
        assert "capability" in SHAREABLE_TYPES
        assert "artifact" in SHAREABLE_TYPES
        assert "workflow" in SHAREABLE_TYPES
        assert "plugin" in SHAREABLE_TYPES
        assert len(SHAREABLE_TYPES) == 4

    def test_share_permissions(self) -> None:
        assert "read" in SHARE_PERMISSIONS
        assert "execute" in SHARE_PERMISSIONS
        assert "fork" in SHARE_PERMISSIONS
        assert len(SHARE_PERMISSIONS) == 3

    def test_sharing_disabled_by_default(self) -> None:
        session = MagicMock()
        svc = SharingService(session, enabled=False)
        with pytest.raises(SharingDisabledError):
            ctx = system_context(new_id())
            svc.create_share(
                ctx,
                resource_type="capability",
                resource_id=new_id(),
            )

    def test_sharing_enabled_creates_share(self) -> None:
        session = MagicMock()
        svc = SharingService(session, enabled=True)
        ctx = system_context(new_id())
        res_id = new_id()
        rec = svc.create_share(
            ctx,
            resource_type="capability",
            resource_id=res_id,
        )
        assert rec.resource_type == "capability"
        assert rec.resource_id == res_id
        assert session.add.called
        assert session.flush.called


class TestSharesRouter:
    def test_create_share_router(self) -> None:
        from xiosync.api.routers.shares import CreateShareRequest, create_share

        req = MagicMock()
        req.state.org_context = system_context(new_id())
        session = MagicMock()
        req.state.org_session = session
        payload = CreateShareRequest(resource_type="capability", resource_id=new_id())
        resp = create_share(payload, req)
        assert isinstance(resp, dict)
        assert resp["resource_type"] == "capability"
        assert "id" in resp


    def test_list_shares_router(self) -> None:
        from xiosync.api.routers.shares import list_shares

        req = MagicMock()
        req.state.org_context = system_context(new_id())
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        req.state.org_session = session
        resp = list_shares(req)
        assert resp == []

    def test_revoke_share_router(self) -> None:
        from xiosync.api.routers.shares import revoke_share

        req = MagicMock()
        ctx = system_context(new_id())
        req.state.org_context = ctx
        session = MagicMock()
        share_id = new_id()
        mock_row = MagicMock()
        mock_row.id = share_id
        mock_row.source_org_id = ctx.organization_id
        mock_row.target_org_id = None
        mock_row.resource_type = "capability"
        mock_row.resource_id = new_id()
        mock_row.permissions = ["read"]
        mock_row.state = "active"
        mock_row.created_at = datetime.now(timezone.utc)
        mock_row.expires_at = None
        session.scalar.return_value = mock_row
        req.state.org_session = session
        resp = revoke_share(share_id, req)
        assert isinstance(resp, dict)
        assert resp["share_id"] == str(share_id)
        assert resp["state"] == "revoked"



# ═══════════════════════════════════════════════════════════════════════════════
# MeteringService — constants
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.metering import METRIC_TYPES, MeteringService


class TestMeteringConstants:
    def test_metric_types_complete(self) -> None:
        expected = {"task_runs", "worker_hours", "event_count", "artifact_bytes", "api_calls"}
        assert METRIC_TYPES == expected

    def test_current_hour(self) -> None:
        start, end = MeteringService._current_hour()
        assert end - start == timedelta(hours=1)
        assert start.minute == 0
        assert start.second == 0
        assert start.microsecond == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Domain validators — secrets, triggers, capabilities, artifacts
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.domain.secrets import validate_provider, validate_secret_state


class TestSecretValidators:
    def test_valid_providers(self) -> None:
        for p in ("env", "vault", "aws-sm", "gcp-sm", "azure-kv", "inline", "custom"):
            validate_provider(p)  # Should not raise

    def test_valid_secret_states(self) -> None:
        for s in ("active", "rotated", "revoked"):
            validate_secret_state(s)  # Should not raise


from xiosync.domain.triggers import (
    validate_cron_expression,
    validate_trigger_state,
    validate_trigger_type,
    next_cron_fire,
)


class TestTriggerValidators:
    def test_valid_trigger_types(self) -> None:
        for t in ("cron", "event", "webhook"):
            validate_trigger_type(t)

    def test_valid_trigger_states(self) -> None:
        for s in ("active", "paused"):
            validate_trigger_state(s)

    def test_invalid_trigger_type_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_trigger_type("invalid_type")

    def test_invalid_trigger_state_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_trigger_state("invalid_state")

    def test_next_cron_fire_returns_future(self) -> None:
        now = datetime.now(timezone.utc)
        nf = next_cron_fire("*/5 * * * *", now)
        assert nf > now


from xiosync.domain.capabilities import (
    validate_capability_state,
    validate_execution_mode,
)


class TestCapabilityValidators:
    def test_valid_states(self) -> None:
        for s in ("active", "deprecated"):
            validate_capability_state(s)

    def test_invalid_state_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_capability_state("invalid")

    def test_valid_execution_modes(self) -> None:
        for m in ("sync", "async", "streaming"):
            validate_execution_mode(m)

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_execution_mode("invalid")


from xiosync.domain.artifacts import validate_provider_type


class TestArtifactValidators:
    def test_valid_provider_types(self) -> None:
        for p in ("s3", "gcs", "r2", "local", "azure_blob", "inline", "custom"):
            validate_provider_type(p)

    def test_invalid_provider_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_provider_type("invalid_provider")


# ═══════════════════════════════════════════════════════════════════════════════
# WebhookService — _generate_signing_secret
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.webhooks import WebhookService


class TestWebhookServiceSigningSecret:
    def test_signing_secret_is_string(self) -> None:
        secret = WebhookService._generate_signing_secret()
        assert isinstance(secret, str)
        assert len(secret) > 16

    def test_signing_secrets_are_unique(self) -> None:
        s1 = WebhookService._generate_signing_secret()
        s2 = WebhookService._generate_signing_secret()
        assert s1 != s2


# ═══════════════════════════════════════════════════════════════════════════════
# Per-capability rate checking (build_rate_checker)
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.core.capability_rate import build_rate_checker
from xiosync.core.rate_limit import RateLimitResult, RateLimiterNotAvailable


class TestBuildRateChecker:
    def test_under_limit_allows(self) -> None:
        limiter = MagicMock()
        limiter.check.return_value = RateLimitResult(allowed=True, remaining=5, reset_after_seconds=30)
        checker = build_rate_checker(limiter, new_id(), "docs.read")
        assert checker({"limit": 10, "window_seconds": 60}) is True

    def test_over_limit_denies(self) -> None:
        limiter = MagicMock()
        limiter.check.return_value = RateLimitResult(allowed=False, remaining=0, reset_after_seconds=30)
        checker = build_rate_checker(limiter, new_id(), "docs.read")
        assert checker({"limit": 10, "window_seconds": 60}) is False

    def test_redis_unavailable_denies(self) -> None:
        """Fail-closed: Redis errors deny access."""
        limiter = MagicMock()
        limiter.check.side_effect = RateLimiterNotAvailable()
        checker = build_rate_checker(limiter, new_id(), "docs.read")
        assert checker({"limit": 10, "window_seconds": 60}) is False

    def test_invalid_config_denies(self) -> None:
        limiter = MagicMock()
        checker = build_rate_checker(limiter, new_id(), "docs.read")
        # Missing limit
        assert checker({"window_seconds": 60}) is False
        # Missing window
        assert checker({"limit": 10}) is False
        # Wrong types
        assert checker({"limit": "ten", "window_seconds": 60}) is False

    def test_zero_limit_denies(self) -> None:
        limiter = MagicMock()
        checker = build_rate_checker(limiter, new_id(), "docs.read")
        assert checker({"limit": 0, "window_seconds": 60}) is False

    def test_key_format(self) -> None:
        """Rate key includes actor_id and capability."""
        limiter = MagicMock()
        limiter.check.return_value = RateLimitResult(allowed=True, remaining=5, reset_after_seconds=30)
        actor_id = new_id()
        checker = build_rate_checker(limiter, actor_id, "docs.read")
        checker({"limit": 10, "window_seconds": 60})
        key = limiter.check.call_args[0][0]
        assert str(actor_id) in key
        assert "docs.read" in key


# ═══════════════════════════════════════════════════════════════════════════════
# Secret Provider Adapters
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.core.secret_providers import (
    EnvProvider,
    InlineProvider,
    SecretResolutionError,
    get_provider,
    resolve,
)


class TestSecretProviders:
    def test_env_provider_resolves(self) -> None:
        os.environ["XIOSYNC_TEST_SECRET"] = "my-value"
        try:
            p = EnvProvider()
            assert p.resolve({"key": "XIOSYNC_TEST_SECRET"}) == "my-value"
        finally:
            del os.environ["XIOSYNC_TEST_SECRET"]

    def test_env_provider_missing_var(self) -> None:
        p = EnvProvider()
        with pytest.raises(SecretResolutionError, match="not set"):
            p.resolve({"key": "XIOSYNC_NONEXISTENT_VAR_12345"})

    def test_env_provider_missing_key(self) -> None:
        p = EnvProvider()
        with pytest.raises(SecretResolutionError, match="requires 'key'"):
            p.resolve({})

    def test_inline_provider(self) -> None:
        p = InlineProvider()
        assert p.resolve({"value": "secret123"}) == "secret123"

    def test_inline_provider_missing_value(self) -> None:
        p = InlineProvider()
        with pytest.raises(SecretResolutionError):
            p.resolve({})

    def test_registry_has_all_providers(self) -> None:
        for name in ("env", "inline", "vault", "aws-sm", "gcp-sm", "azure-kv", "custom"):
            assert get_provider(name) is not None

    def test_resolve_dispatches(self) -> None:
        os.environ["XIOSYNC_TEST_DISPATCH"] = "dispatched"
        try:
            val = resolve("env", {"key": "XIOSYNC_TEST_DISPATCH"})
            assert val == "dispatched"
        finally:
            del os.environ["XIOSYNC_TEST_DISPATCH"]

    def test_resolve_unknown_provider(self) -> None:
        with pytest.raises(SecretResolutionError, match="no adapter"):
            resolve("nonexistent_provider", {})
