"""SessionService — the C8 session lifecycle (doc 05 §2.2).

Implements exactly the lifecycle doc 05 §2.2 draws:

- ``login``: constant-time credential check with lockout, then one server-side
  ``sessions`` row, a ≤15-minute access token, and an opaque rotating refresh
  token (only its SHA-256 hash is stored).
- ``refresh``: rotate the refresh hash; a rotated token presented again
  revokes the whole session and emits a ``critical`` ``auth_event``
  (INV-SESSION-3 — token-theft detection).
- ``logout`` / ``revoke_all_sessions``: the kill switch (INV-SESSION-2).
- ``validate_access_token``: signature + lifetime via ``platform/tokens``,
  then the mandatory live-session-row check (INV-SESSION-1) and resolution
  into a frozen ``OrgContext`` (C1) — never a context from token claims alone.

Every failure raises the single ``AuthenticationError`` with one message:
callers (and attackers) never learn *why* authentication failed (no oracle).

Refresh tokens are structured ``<session_id>.<org_id>.<secret>`` so the
service can locate the session row without any cross-org scan; the embedded
ids grant nothing — only the stored hash of the full token does, compared
constant-time. Comparison uses SHA-256 (the token carries 256 bits of
entropy; key-stretching is for low-entropy passwords, which use argon2id).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.identity import IdentityRecord, IdentityRepository
from xiosync.platform.crypto import constant_time_equals, verify_password
from xiosync.platform.ids import new_id
from xiosync.platform.telemetry import get_logger
from xiosync.platform.tokens import (
    DEFAULT_ACCESS_TOKEN_TTL,
    TokenError,
    issue_access_token,
    verify_access_token,
)

#: Failed attempts that trip the lockout (doc 05 §2.1 lockout policy).
LOCKOUT_THRESHOLD = 5
#: How long a tripped lockout lasts.
LOCKOUT_DURATION = timedelta(minutes=15)
#: Server-side session (refresh-token) lifetime.
SESSION_TTL = timedelta(days=14)

_IDENTITY_STATE_ACTIVE = "active"
_SESSION_STATE_ACTIVE = "active"

_logger = get_logger("xiosync.services.identity")


class AuthenticationError(Exception):
    """Authentication failed. One error, one message — no oracle."""

    def __init__(self) -> None:
        super().__init__("authentication failed")


@dataclass(frozen=True, slots=True)
class TokenPair:
    """What a successful login or refresh hands the transport layer."""

    access_token: str
    access_token_expires_at: datetime
    refresh_token: str
    session_id: uuid.UUID
    organization_id: uuid.UUID


def _mint_refresh_token(session_id: uuid.UUID, organization_id: uuid.UUID) -> str:
    return f"{session_id}.{organization_id}.{secrets.token_urlsafe(32)}"


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_refresh_token(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Extract ``(session_id, organization_id)`` or raise (single path)."""
    try:
        session_part, org_part, secret_part = token.split(".", 2)
        if not secret_part:
            raise AuthenticationError
        return uuid.UUID(session_part), uuid.UUID(org_part)
    except (ValueError, AttributeError) as exc:
        raise AuthenticationError from exc


class SessionService:
    """Use-case orchestration for the session lifecycle (doc 04 §2.2)."""

    def __init__(self, repository: IdentityRepository, auth_secret: str) -> None:
        if not auth_secret:
            raise ValueError("auth_secret must not be empty")
        self._repository = repository
        self._auth_secret = auth_secret

    # -- login ------------------------------------------------------------

    def login(
        self, organization_id: uuid.UUID, email: str, password: str, *, now: datetime
    ) -> TokenPair:
        """Validate credentials; create a session; issue both tokens."""
        identity = self._repository.find_identity_by_email(organization_id, email)
        if identity is None:
            # Equalize timing with a real verification against a dummy hash.
            verify_password(_DUMMY_HASH, password)
            raise AuthenticationError
        self._check_identity_usable(identity, now=now)

        if not verify_password(identity.password_hash, password):
            self._register_failure(identity, now=now)
            raise AuthenticationError

        if identity.failed_attempts:
            self._repository.reset_failed_attempts(organization_id, identity.id, now=now)

        # Membership is required to act in the org (doc 05 §2; doc 03 §5).
        if self._repository.get_membership_role(organization_id, identity.id) is None:
            raise AuthenticationError

        return self._open_session(identity, now=now)

    def _check_identity_usable(self, identity: IdentityRecord, *, now: datetime) -> None:
        if identity.state != _IDENTITY_STATE_ACTIVE:
            raise AuthenticationError
        if identity.locked_until is not None and identity.locked_until > now:
            raise AuthenticationError

    def _register_failure(self, identity: IdentityRecord, *, now: datetime) -> None:
        attempts = identity.failed_attempts + 1
        locked_until = now + LOCKOUT_DURATION if attempts >= LOCKOUT_THRESHOLD else None
        self._repository.record_failed_attempt(
            identity.organization_id,
            identity.id,
            failed_attempts=attempts,
            locked_until=locked_until,
            now=now,
        )
        if locked_until is not None:
            _logger.warning(
                "auth_event: lockout tripped",
                extra={"auth_identity_id": str(identity.id)},
            )

    def _open_session(self, identity: IdentityRecord, *, now: datetime) -> TokenPair:
        session_id = new_id()
        refresh_token = _mint_refresh_token(session_id, identity.organization_id)
        self._repository.create_session(
            identity.organization_id,
            identity.id,
            session_id=session_id,
            refresh_token_hash=_hash_refresh_token(refresh_token),
            expires_at=now + SESSION_TTL,
        )
        access_token, claims = issue_access_token(
            self._auth_secret,
            session_id=session_id,
            organization_id=identity.organization_id,
            actor_id=identity.human_actor_id,
            now=now,
            ttl=DEFAULT_ACCESS_TOKEN_TTL,
        )
        return TokenPair(
            access_token=access_token,
            access_token_expires_at=claims.expires_at,
            refresh_token=refresh_token,
            session_id=session_id,
            organization_id=identity.organization_id,
        )

    # -- refresh (rotation + reuse-revoke) ---------------------------------

    def refresh(self, refresh_token: str, *, now: datetime) -> TokenPair:
        """Rotate the refresh token; reuse revokes the family (INV-SESSION-3)."""
        session_id, organization_id = _parse_refresh_token(refresh_token)
        session = self._repository.get_session(organization_id, session_id)
        if session is None or session.expires_at <= now:
            raise AuthenticationError

        presented = _hash_refresh_token(refresh_token)
        hashes_match = constant_time_equals(
            presented.encode("utf-8"), session.refresh_token_hash.encode("utf-8")
        )

        if session.state != _SESSION_STATE_ACTIVE:
            raise AuthenticationError

        if not hashes_match:
            # An active session presented with a non-current (rotated) token:
            # the old token leaked. Revoke the whole family and alert.
            self._repository.revoke_session(organization_id, session_id, now=now)
            _logger.critical(
                "auth_event: refresh-token reuse detected — session family revoked",
                extra={"session_id": str(session_id)},
            )
            raise AuthenticationError

        identity = self._repository.get_identity(organization_id, session.auth_identity_id)
        if identity is None:
            raise AuthenticationError
        self._check_identity_usable(identity, now=now)

        new_refresh_token = _mint_refresh_token(session_id, organization_id)
        rotated = self._repository.rotate_refresh_token_hash(
            organization_id,
            session_id,
            expected_refresh_token_hash=presented,
            refresh_token_hash=_hash_refresh_token(new_refresh_token),
            now=now,
        )
        if not rotated:
            # Another presenter won the compare-and-swap. The token was reused
            # concurrently, so revoke the family exactly as for serial reuse.
            self._repository.revoke_session(organization_id, session_id, now=now)
            _logger.critical(
                "auth_event: concurrent refresh-token reuse detected — session family revoked",
                extra={"session_id": str(session_id)},
            )
            raise AuthenticationError

        access_token, claims = issue_access_token(
            self._auth_secret,
            session_id=session_id,
            organization_id=organization_id,
            actor_id=identity.human_actor_id,
            now=now,
            ttl=DEFAULT_ACCESS_TOKEN_TTL,
        )
        return TokenPair(
            access_token=access_token,
            access_token_expires_at=claims.expires_at,
            refresh_token=new_refresh_token,
            session_id=session_id,
            organization_id=organization_id,
        )

    # -- revocation (the C8 kill switch) -----------------------------------

    def logout(self, context: OrgContext, *, now: datetime) -> None:
        """Revoke the context's own session (doc 05 §2.2 logout)."""
        self._repository.revoke_session(context.organization_id, context.session_id, now=now)

    def revoke_all_sessions(
        self, organization_id: uuid.UUID, auth_identity_id: uuid.UUID, *, now: datetime
    ) -> int:
        """Revoke every session of one identity (INV-SESSION-2)."""
        return self._repository.revoke_all_sessions(organization_id, auth_identity_id, now=now)

    # -- validation (INV-SESSION-1) ----------------------------------------

    def validate_access_token(self, access_token: str, *, now: datetime) -> OrgContext:
        """Verify the token AND its live session row; resolve OrgContext.

        A valid signature alone never grants access: the referenced session
        must exist, be ``active``, and be unexpired (INV-SESSION-1 — the kill
        switch). The membership role comes from the database, not the token.
        """
        try:
            claims = verify_access_token(self._auth_secret, access_token, now=now)
        except TokenError as exc:
            raise AuthenticationError from exc

        session = self._repository.get_session(claims.organization_id, claims.session_id)
        if session is None or session.state != _SESSION_STATE_ACTIVE or session.expires_at <= now:
            raise AuthenticationError

        identity = self._repository.get_identity(claims.organization_id, session.auth_identity_id)
        if identity is None or identity.human_actor_id != claims.actor_id:
            raise AuthenticationError
        self._check_identity_usable(identity, now=now)

        role = self._repository.get_membership_role(claims.organization_id, identity.id)
        if role is None:
            raise AuthenticationError

        try:
            return OrgContext(
                auth_identity_id=identity.id,
                actor_id=identity.human_actor_id,
                organization_id=claims.organization_id,
                session_id=claims.session_id,
                platform_role=PlatformRole.NONE,
                membership_role=MembershipRole(role),
            )
        except ValueError as exc:
            raise AuthenticationError from exc


# A real argon2id hash of an unguessable value, used only to equalize login
# timing when the email is unknown (never a credential).
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0c2FsdA$"
    "n2qzZ8h1sTfXBROYCiwFHiIB4rk8Ffz1sJEE2AiEXVQ"
)
