"""Real-PostgreSQL tests for the rev-0002 session lifecycle and RLS path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from xiosync.persistence.identity import IdentityRepository
from xiosync.platform.crypto import hash_password
from xiosync.platform.ids import new_id
from xiosync.services.identity import AuthenticationError, SessionService

pytestmark = [pytest.mark.integration, pytest.mark.security]

_NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
_SECRET = "integration-test-signing-secret-at-least-32-bytes"
_PASSWORD = "correct horse battery staple"


def _seed_identity(admin_url: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    organization_id = new_id()
    actor_id = new_id()
    identity_id = new_id()
    membership_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'Session Test', 'active')"
                ),
                {"id": organization_id, "slug": f"session-{organization_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO actors (id, organization_id, actor_type, state, "
                    "lifecycle_phase, trust_tier, health_status) VALUES "
                    "(:id, :org, 'human', 'active', 'operational', 'newcomer', 'healthy')"
                ),
                {"id": actor_id, "org": organization_id},
            )
            connection.execute(
                text(
                    "INSERT INTO auth_identities "
                    "(id, organization_id, human_actor_id, email, password_hash, state) "
                    "VALUES (:id, :org, :actor, 'owner@example.test', :password, 'active')"
                ),
                {
                    "id": identity_id,
                    "org": organization_id,
                    "actor": actor_id,
                    "password": hash_password(_PASSWORD),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO memberships "
                    "(id, organization_id, auth_identity_id, membership_role) "
                    "VALUES (:id, :org, :identity, 'org_owner')"
                ),
                {"id": membership_id, "org": organization_id, "identity": identity_id},
            )
    finally:
        engine.dispose()
    return organization_id, actor_id, identity_id


def test_login_refresh_reuse_revoke_and_live_validation(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    organization_id, actor_id, _ = _seed_identity(migrated_database_url)
    engine = create_engine(app_role_database_url)
    try:
        service = SessionService(IdentityRepository(engine), _SECRET)
        login = service.login(organization_id, "owner@example.test", _PASSWORD, now=_NOW)
        context = service.validate_access_token(login.access_token, now=_NOW + timedelta(minutes=1))
        assert context.organization_id == organization_id
        assert context.actor_id == actor_id

        rotated = service.refresh(login.refresh_token, now=_NOW + timedelta(minutes=2))
        service.validate_access_token(rotated.access_token, now=_NOW + timedelta(minutes=3))

        # INV-SESSION-3: replaying the old refresh token revokes the family.
        with pytest.raises(AuthenticationError, match="^authentication failed$"):
            service.refresh(login.refresh_token, now=_NOW + timedelta(minutes=4))

        # INV-SESSION-1: both otherwise-valid access tokens die immediately.
        with pytest.raises(AuthenticationError):
            service.validate_access_token(rotated.access_token, now=_NOW + timedelta(minutes=5))
    finally:
        engine.dispose()


def test_logout_and_revoke_all_invalidate_live_access_tokens(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    organization_id, _, identity_id = _seed_identity(migrated_database_url)
    engine = create_engine(app_role_database_url)
    try:
        service = SessionService(IdentityRepository(engine), _SECRET)
        first = service.login(organization_id, "owner@example.test", _PASSWORD, now=_NOW)
        first_context = service.validate_access_token(
            first.access_token, now=_NOW + timedelta(minutes=1)
        )
        service.logout(first_context, now=_NOW + timedelta(minutes=2))
        with pytest.raises(AuthenticationError):
            service.validate_access_token(first.access_token, now=_NOW + timedelta(minutes=3))

        second = service.login(
            organization_id,
            "owner@example.test",
            _PASSWORD,
            now=_NOW + timedelta(minutes=4),
        )
        third = service.login(
            organization_id,
            "owner@example.test",
            _PASSWORD,
            now=_NOW + timedelta(minutes=5),
        )
        assert (
            service.revoke_all_sessions(
                organization_id, identity_id, now=_NOW + timedelta(minutes=6)
            )
            == 2
        )
        for token in (second.access_token, third.access_token):
            with pytest.raises(AuthenticationError):
                service.validate_access_token(token, now=_NOW + timedelta(minutes=7))
    finally:
        engine.dispose()
