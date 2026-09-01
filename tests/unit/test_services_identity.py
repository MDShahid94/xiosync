"""Unit tests for the C8 session lifecycle service (doc 05 §2.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import create_autospec

import pytest
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.identity import IdentityRecord, IdentityRepository, SessionRecord
from xiosync.platform.crypto import hash_password
from xiosync.platform.tokens import issue_access_token
from xiosync.services.identity import (
    LOCKOUT_THRESHOLD,
    AuthenticationError,
    SessionService,
)

_NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
_SECRET = "unit-test-signing-secret-at-least-32-bytes"
_ORG_ID = uuid.uuid4()
_IDENTITY_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()


def _identity(*, failed_attempts: int = 0, state: str = "active") -> IdentityRecord:
    return IdentityRecord(
        id=_IDENTITY_ID,
        organization_id=_ORG_ID,
        human_actor_id=_ACTOR_ID,
        email="owner@example.test",
        password_hash=hash_password("correct horse battery staple"),
        state=state,
        failed_attempts=failed_attempts,
        locked_until=None,
    )


def _repository() -> Any:
    return create_autospec(IdentityRepository, instance=True)


def _context() -> OrgContext:
    return OrgContext(
        auth_identity_id=_IDENTITY_ID,
        actor_id=_ACTOR_ID,
        organization_id=_ORG_ID,
        session_id=_SESSION_ID,
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_OWNER,
    )


def test_login_creates_server_session_and_issues_tokens() -> None:
    repository = _repository()
    repository.find_identity_by_email.return_value = _identity()
    repository.get_membership_role.return_value = "org_owner"
    service = SessionService(repository, _SECRET)

    pair = service.login(_ORG_ID, "owner@example.test", "correct horse battery staple", now=_NOW)

    assert pair.organization_id == _ORG_ID
    assert pair.refresh_token.startswith(f"{pair.session_id}.{_ORG_ID}.")
    repository.create_session.assert_called_once()
    stored_hash = repository.create_session.call_args.kwargs["refresh_token_hash"]
    assert stored_hash != pair.refresh_token
    assert pair.access_token_expires_at <= _NOW + timedelta(minutes=15)


def test_bad_password_at_threshold_locks_identity() -> None:
    repository = _repository()
    repository.find_identity_by_email.return_value = _identity(
        failed_attempts=LOCKOUT_THRESHOLD - 1
    )
    service = SessionService(repository, _SECRET)

    with pytest.raises(AuthenticationError, match="^authentication failed$"):
        service.login(_ORG_ID, "owner@example.test", "wrong", now=_NOW)

    assert repository.record_failed_attempt.call_args.kwargs["failed_attempts"] == LOCKOUT_THRESHOLD
    assert repository.record_failed_attempt.call_args.kwargs["locked_until"] > _NOW
    repository.create_session.assert_not_called()


def test_refresh_rotates_hash_and_old_token_reuse_revokes_family() -> None:
    repository = _repository()
    service = SessionService(repository, _SECRET)
    first = service._open_session(_identity(), now=_NOW)
    initial_hash = repository.create_session.call_args.kwargs["refresh_token_hash"]
    repository.get_session.return_value = SessionRecord(
        id=first.session_id,
        organization_id=_ORG_ID,
        auth_identity_id=_IDENTITY_ID,
        refresh_token_hash=initial_hash,
        state="active",
        expires_at=_NOW + timedelta(days=1),
    )
    repository.get_identity.return_value = _identity()
    repository.rotate_refresh_token_hash.return_value = True

    rotated = service.refresh(first.refresh_token, now=_NOW + timedelta(minutes=1))

    assert rotated.refresh_token != first.refresh_token
    repository.rotate_refresh_token_hash.assert_called_once()
    repository.get_session.return_value = SessionRecord(
        id=first.session_id,
        organization_id=_ORG_ID,
        auth_identity_id=_IDENTITY_ID,
        refresh_token_hash=repository.rotate_refresh_token_hash.call_args.kwargs[
            "refresh_token_hash"
        ],
        state="active",
        expires_at=_NOW + timedelta(days=1),
    )

    with pytest.raises(AuthenticationError):
        service.refresh(first.refresh_token, now=_NOW + timedelta(minutes=2))

    repository.revoke_session.assert_called_once_with(
        _ORG_ID, first.session_id, now=_NOW + timedelta(minutes=2)
    )


def test_concurrent_refresh_compare_and_swap_loss_revokes_family() -> None:
    repository = _repository()
    service = SessionService(repository, _SECRET)
    pair = service._open_session(_identity(), now=_NOW)
    repository.get_session.return_value = SessionRecord(
        id=pair.session_id,
        organization_id=_ORG_ID,
        auth_identity_id=_IDENTITY_ID,
        refresh_token_hash=repository.create_session.call_args.kwargs["refresh_token_hash"],
        state="active",
        expires_at=_NOW + timedelta(days=1),
    )
    repository.get_identity.return_value = _identity()
    repository.rotate_refresh_token_hash.return_value = False

    with pytest.raises(AuthenticationError):
        service.refresh(pair.refresh_token, now=_NOW + timedelta(minutes=1))

    repository.revoke_session.assert_called_once_with(
        _ORG_ID, pair.session_id, now=_NOW + timedelta(minutes=1)
    )


def test_validate_requires_live_row_and_resolves_database_role() -> None:
    repository = _repository()
    service = SessionService(repository, _SECRET)
    token, _ = issue_access_token(
        _SECRET,
        session_id=_SESSION_ID,
        organization_id=_ORG_ID,
        actor_id=_ACTOR_ID,
        now=_NOW,
    )
    repository.get_session.return_value = SessionRecord(
        id=_SESSION_ID,
        organization_id=_ORG_ID,
        auth_identity_id=_IDENTITY_ID,
        refresh_token_hash="hash",
        state="active",
        expires_at=_NOW + timedelta(days=1),
    )
    repository.get_identity.return_value = _identity()
    repository.get_membership_role.return_value = "org_admin"

    context = service.validate_access_token(token, now=_NOW + timedelta(minutes=1))

    assert context.auth_identity_id == _IDENTITY_ID
    assert context.actor_id == _ACTOR_ID
    assert context.membership_role is MembershipRole.ORG_ADMIN
    repository.get_session.assert_called_once_with(_ORG_ID, _SESSION_ID)

    repository.get_session.return_value = SessionRecord(
        id=_SESSION_ID,
        organization_id=_ORG_ID,
        auth_identity_id=_IDENTITY_ID,
        refresh_token_hash="hash",
        state="revoked",
        expires_at=_NOW + timedelta(days=1),
    )
    with pytest.raises(AuthenticationError):
        service.validate_access_token(token, now=_NOW + timedelta(minutes=1))


def test_logout_and_revoke_all_delegate_to_kill_switches() -> None:
    repository = _repository()
    repository.revoke_all_sessions.return_value = 3
    service = SessionService(repository, _SECRET)

    service.logout(_context(), now=_NOW)
    count = service.revoke_all_sessions(_ORG_ID, _IDENTITY_ID, now=_NOW)

    repository.revoke_session.assert_called_once_with(_ORG_ID, _SESSION_ID, now=_NOW)
    repository.revoke_all_sessions.assert_called_once_with(_ORG_ID, _IDENTITY_ID, now=_NOW)
    assert count == 3
