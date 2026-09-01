"""Integration tests for Wave 3 services: quotas, triggers, webhooks,
metering, secrets, capabilities, and artifacts.

Each test runs against a real PostgreSQL scratch database migrated by Alembic.
Seeding uses the admin connection; service calls use org_scoped_session.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id

pytestmark = pytest.mark.integration


# ── Shared Helpers ───────────────────────────────────────────────────────────

def _seed_org_actor(admin_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one active org + actor; return (org_id, actor_id)."""
    org_id = new_id()
    actor_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'SvcTest', 'active')"
                ),
                {"id": org_id, "slug": f"svc-{org_id}"},
            )
            conn.execute(
                text(
                    "INSERT INTO actors (id, organization_id, actor_type, state, "
                    "lifecycle_phase, trust_tier, health_status) VALUES "
                    "(:id, :org, 'human', 'active', 'operational', 'trusted', 'healthy')"
                ),
                {"id": actor_id, "org": org_id},
            )
    finally:
        engine.dispose()
    return org_id, actor_id


def _ctx(org_id: uuid.UUID, actor_id: uuid.UUID) -> OrgContext:
    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=actor_id,
        organization_id=org_id,
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# QuotaService
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.quotas import QuotaExceededError, QuotaService


class TestQuotaServiceIntegration:
    def test_no_quota_allows(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = QuotaService(session)
                svc.check_workers(org_id)
                svc.check_queued_tasks(org_id)
                svc.check_concurrent_runs(org_id)
                svc.check_daily_events(org_id)
        finally:
            engine.dispose()

    def test_quota_with_explicit_limits(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        # Set quotas on the org
        engine = create_engine(migrated_database_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE organizations SET resource_quotas = "
                        "'{\"max_workers\": 100, \"max_queued_tasks\": 1000}' "
                        "WHERE id = :id"
                    ),
                    {"id": org_id},
                )
            with org_scoped_session(engine, ctx) as session:
                svc = QuotaService(session)
                # Under limit — should pass
                svc.check_workers(org_id)
                svc.check_queued_tasks(org_id)
        finally:
            engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# TriggerService
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.triggers import TriggerNotFoundError, TriggerService


class TestTriggerServiceIntegration:
    def _seed_workflow(self, admin_url: str, org_id: uuid.UUID, actor_id: uuid.UUID) -> uuid.UUID:
        wf_id = new_id()
        engine = create_engine(admin_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO workflows (id, organization_id, name, state, created_by, version) "
                        "VALUES (:id, :org, 'test-wf', 'draft', :actor, 1)"
                    ),
                    {"id": wf_id, "org": org_id, "actor": actor_id},
                )
        finally:
            engine.dispose()
        return wf_id

    def test_create_and_get_cron_trigger(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        wf_id = self._seed_workflow(migrated_database_url, org_id, actor_id)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = TriggerService(session)
                rec = svc.create_trigger(
                    ctx,
                    workflow_id=wf_id,
                    trigger_type="cron",
                    config={"cron": "*/5 * * * *"},
                    created_by=actor_id,
                )
                assert rec.trigger_type == "cron"
                assert rec.state == "active"
                assert rec.next_fire_at is not None

            # Re-open session to verify persistence
            with org_scoped_session(engine, ctx) as session:
                svc = TriggerService(session)
                fetched = svc.get_trigger(ctx, rec.id)
                assert fetched.id == rec.id
        finally:
            engine.dispose()

    def test_pause_and_resume_trigger(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        wf_id = self._seed_workflow(migrated_database_url, org_id, actor_id)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = TriggerService(session)
                rec = svc.create_trigger(
                    ctx,
                    workflow_id=wf_id,
                    trigger_type="event",
                    config={"event_type": "task.completed"},
                    created_by=actor_id,
                )
                paused = svc.pause_trigger(ctx, rec.id)
                assert paused.state == "paused"
                resumed = svc.resume_trigger(ctx, rec.id)
                assert resumed.state == "active"
        finally:
            engine.dispose()

    def test_list_triggers(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        wf_id = self._seed_workflow(migrated_database_url, org_id, actor_id)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = TriggerService(session)
                svc.create_trigger(ctx, workflow_id=wf_id, trigger_type="cron",
                                   config={"cron": "0 * * * *"}, created_by=actor_id)
                svc.create_trigger(ctx, workflow_id=wf_id, trigger_type="event",
                                   config={"event_type": "task.completed"}, created_by=actor_id)
                items = svc.list_triggers(ctx)
                assert len(items) >= 2
        finally:
            engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# WebhookService
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.webhooks import WebhookService


class TestWebhookServiceIntegration:
    def test_create_and_list_subscription(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WebhookService(session)
                rec = svc.create_subscription(
                    ctx,
                    url="https://example.com/hook",
                    event_types=["task.completed", "workflow.completed"],
                )
                assert rec.url == "https://example.com/hook"
                assert rec.state == "active"
                assert len(rec.signing_secret) > 16

            with org_scoped_session(engine, ctx) as session:
                svc = WebhookService(session)
                items = svc.list_subscriptions(ctx)
                assert len(items) >= 1
                assert items[0].url == "https://example.com/hook"
        finally:
            engine.dispose()

    def test_matching_subscriptions(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WebhookService(session)
                svc.create_subscription(ctx, url="https://a.com/hook",
                                        event_types=["task.completed"])
                svc.create_subscription(ctx, url="https://b.com/hook",
                                        event_types=["workflow.completed"])
                matches = svc.get_matching_subscriptions(ctx, "task.completed")
                assert len(matches) == 1
                assert matches[0].url == "https://a.com/hook"
        finally:
            engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# MeteringService
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.metering import MeteringService


class TestMeteringServiceIntegration:
    def test_record_and_get_usage(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = MeteringService(session)
                rec = svc.record_usage(ctx, metric_type="task_runs", value=5)
                assert rec.metric_type == "task_runs"
                assert rec.value == 5
                svc.record_usage(ctx, metric_type="api_calls", value=100)
                # Verify via get_usage
                records = svc.get_usage(ctx, metric_type="task_runs")
                assert len(records) == 1
                assert records[0].value == 5
        finally:
            engine.dispose()

    def test_upsert_same_hour_accumulates(self, migrated_database_url: str) -> None:
        """Two record_usage calls in the same hour merge into one row."""
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = MeteringService(session)
                svc.record_usage(ctx, metric_type="event_count", value=10)
                rec = svc.record_usage(ctx, metric_type="event_count", value=20)
                # Upsert: 10 + 20 = 30
                assert rec.value == 30
                records = svc.get_usage(ctx, metric_type="event_count")
                assert len(records) == 1
                assert records[0].value == 30
        finally:
            engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# SecretRefService
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.secrets import SecretNotFoundError, SecretRefService


class TestSecretRefServiceIntegration:
    def test_create_and_get_secret(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = SecretRefService(session)
                rec = svc.create_secret(
                    ctx,
                    name="my-api-key",
                    provider="vault",
                    ref_config={"path": "secret/data/api-key"},
                    created_by=actor_id,
                )
                assert rec.name == "my-api-key"
                assert rec.provider == "vault"
                assert rec.state == "active"

            with org_scoped_session(engine, ctx) as session:
                svc = SecretRefService(session)
                fetched = svc.get_secret(ctx, rec.id)
                assert fetched.name == "my-api-key"
        finally:
            engine.dispose()

    def test_get_by_name(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = SecretRefService(session)
                svc.create_secret(ctx, name="db-password", provider="env",
                                  ref_config={"key": "DB_PASS"}, created_by=actor_id)
                found = svc.get_secret_by_name(ctx, "db-password")
                assert found is not None
                assert found.name == "db-password"
                not_found = svc.get_secret_by_name(ctx, "nonexistent")
                assert not_found is None
        finally:
            engine.dispose()

    def test_revoke_secret(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = SecretRefService(session)
                rec = svc.create_secret(ctx, name="temp-key", provider="env",
                                        ref_config={"key": "TMP"}, created_by=actor_id)
                revoked = svc.revoke_secret(ctx, rec.id)
                assert revoked.state == "revoked"
        finally:
            engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# CapabilityService
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.capabilities import CapabilityNotFoundError, CapabilityService


class TestCapabilityServiceIntegration:
    def test_create_and_deprecate_capability(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = CapabilityService(session)
                rec = svc.create_capability(
                    ctx,
                    name="translate-text",
                    description="Translates text between languages",
                    execution_mode="sync",
                    timeout_ms=30000,
                )
                assert rec.name == "translate-text"
                assert rec.state == "active"
                assert rec.execution_mode == "sync"

            with org_scoped_session(engine, ctx) as session:
                svc = CapabilityService(session)
                deprecated = svc.deprecate_capability(ctx, rec.id)
                assert deprecated.state == "deprecated"
        finally:
            engine.dispose()

    def test_get_by_name(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = CapabilityService(session)
                svc.create_capability(ctx, name="unique-cap-name")
                found = svc.get_capability_by_name(ctx, "unique-cap-name")
                assert found is not None
                assert found.name == "unique-cap-name"
        finally:
            engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# ArtifactService
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.services.artifacts import ArtifactNotFoundError, ArtifactService


class TestArtifactServiceIntegration:
    def test_create_and_list_artifacts(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = ArtifactService(session)
                rec = svc.create_artifact(
                    ctx,
                    provider_type="s3",
                    uri="s3://bucket/path/to/file.txt",
                    created_by=actor_id,
                    content_type="text/plain",
                    size_bytes=1234,
                    checksum="sha256:abc123",
                )
                assert rec.provider_type == "s3"
                assert rec.uri == "s3://bucket/path/to/file.txt"

            with org_scoped_session(engine, ctx) as session:
                svc = ArtifactService(session)
                items = svc.list_artifacts(ctx, provider_type="s3")
                assert len(items) >= 1
                assert items[0].uri == "s3://bucket/path/to/file.txt"
        finally:
            engine.dispose()

    def test_get_artifact(self, migrated_database_url: str) -> None:
        org_id, actor_id = _seed_org_actor(migrated_database_url)
        ctx = _ctx(org_id, actor_id)
        engine = create_engine(migrated_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = ArtifactService(session)
                rec = svc.create_artifact(
                    ctx, provider_type="gcs", uri="gs://bucket/file",
                    created_by=actor_id,
                )

            with org_scoped_session(engine, ctx) as session:
                svc = ArtifactService(session)
                fetched = svc.get_artifact(ctx, rec.id)
                assert fetched.uri == "gs://bucket/file"
        finally:
            engine.dispose()
