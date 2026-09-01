"""Identity persistence — session-lifecycle repository (C8; docs 05 §2, 06 §5).

This is the **one sanctioned pre-context module**: login and refresh run
*before* an ``OrgContext`` exists (they are what produces one), so INV-TENANT-3
cannot literally apply here. The compensating control is that every method
still binds the rev-0002 RLS GUC (``app.current_org``) for exactly its own
transaction, parameterized, to an organization id the caller proved from its
input (login carries the org id; a refresh token embeds the one it was minted
for). There is no unscoped query path: with the GUC unbound the RLS policies
return zero rows and reject writes (doc 05 §3.2 layer 2).

The repository returns frozen record dataclasses, never live ORM rows, so the
service layer holds no open session state. Each method is one transaction;
cross-call races (e.g. two concurrent refreshes) resolve at the service layer
by the reuse-revoke rule (INV-SESSION-3), not by long transactions.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import Session as OrmSession

from xiosync.persistence.models.identity import AuthIdentity, Membership
from xiosync.persistence.models.identity import Session as SessionRow
from xiosync.persistence.tenancy import RLS_ORG_SETTING

_SET_ORG_LOCAL = text("SELECT set_config(:setting, :org_id, true)")

_SESSION_STATE_ACTIVE = "active"
_SESSION_STATE_REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    """One ``auth_identities`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    human_actor_id: uuid.UUID
    email: str
    password_hash: str
    state: str
    failed_attempts: int
    locked_until: datetime | None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One ``sessions`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    auth_identity_id: uuid.UUID
    refresh_token_hash: str
    state: str
    expires_at: datetime


def _identity_record(row: AuthIdentity) -> IdentityRecord:
    return IdentityRecord(
        id=row.id,
        organization_id=row.organization_id,
        human_actor_id=row.human_actor_id,
        email=row.email,
        password_hash=row.password_hash,
        state=row.state,
        failed_attempts=row.failed_attempts,
        locked_until=row.locked_until,
    )


def _session_record(row: SessionRow) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        organization_id=row.organization_id,
        auth_identity_id=row.auth_identity_id,
        refresh_token_hash=row.refresh_token_hash,
        state=row.state,
        expires_at=row.expires_at,
    )


class IdentityRepository:
    """All database access for the session lifecycle (doc 05 §2.2)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def _org_transaction(self, organization_id: uuid.UUID) -> Iterator[OrmSession]:
        """One transaction with the RLS GUC bound to ``organization_id``.

        Same fail-closed shape as ``org_scoped_session`` (persistence/tenancy)
        but keyed on a caller-proven org id instead of an ``OrgContext``,
        because these calls are what *create* the context.
        """
        with OrmSession(self._engine) as session, session.begin():
            session.execute(
                _SET_ORG_LOCAL,
                {"setting": RLS_ORG_SETTING, "org_id": str(organization_id)},
            )
            yield session

    def find_identity_by_email(
        self, organization_id: uuid.UUID, email: str
    ) -> IdentityRecord | None:
        """Look up the identity for a login attempt (email unique within org)."""
        with self._org_transaction(organization_id) as session:
            row = session.scalars(
                select(AuthIdentity).where(
                    AuthIdentity.organization_id == organization_id,
                    AuthIdentity.email == email,
                )
            ).one_or_none()
            return None if row is None else _identity_record(row)

    def get_identity(
        self, organization_id: uuid.UUID, auth_identity_id: uuid.UUID
    ) -> IdentityRecord | None:
        with self._org_transaction(organization_id) as session:
            row = session.scalars(
                select(AuthIdentity).where(
                    AuthIdentity.organization_id == organization_id,
                    AuthIdentity.id == auth_identity_id,
                )
            ).one_or_none()
            return None if row is None else _identity_record(row)

    def record_failed_attempt(
        self,
        organization_id: uuid.UUID,
        auth_identity_id: uuid.UUID,
        *,
        failed_attempts: int,
        locked_until: datetime | None,
        now: datetime,
    ) -> None:
        """Persist a failed login: the new counter and (on breach) the lock."""
        with self._org_transaction(organization_id) as session:
            session.execute(
                update(AuthIdentity)
                .where(
                    AuthIdentity.organization_id == organization_id,
                    AuthIdentity.id == auth_identity_id,
                )
                .values(failed_attempts=failed_attempts, locked_until=locked_until, updated_at=now)
            )

    def reset_failed_attempts(
        self, organization_id: uuid.UUID, auth_identity_id: uuid.UUID, *, now: datetime
    ) -> None:
        with self._org_transaction(organization_id) as session:
            session.execute(
                update(AuthIdentity)
                .where(
                    AuthIdentity.organization_id == organization_id,
                    AuthIdentity.id == auth_identity_id,
                )
                .values(failed_attempts=0, locked_until=None, updated_at=now)
            )

    def get_membership_role(
        self, organization_id: uuid.UUID, auth_identity_id: uuid.UUID
    ) -> str | None:
        """The identity's role in this organization, or None if no membership."""
        with self._org_transaction(organization_id) as session:
            return session.scalars(
                select(Membership.membership_role).where(
                    Membership.organization_id == organization_id,
                    Membership.auth_identity_id == auth_identity_id,
                )
            ).one_or_none()

    def create_session(
        self,
        organization_id: uuid.UUID,
        auth_identity_id: uuid.UUID,
        *,
        session_id: uuid.UUID,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> None:
        """Insert one active session row. The id is caller-supplied because
        the refresh token embeds it (it must exist before hashing)."""
        with self._org_transaction(organization_id) as session:
            session.add(
                SessionRow(
                    id=session_id,
                    organization_id=organization_id,
                    auth_identity_id=auth_identity_id,
                    refresh_token_hash=refresh_token_hash,
                    state=_SESSION_STATE_ACTIVE,
                    expires_at=expires_at,
                )
            )

    def get_session(
        self, organization_id: uuid.UUID, session_id: uuid.UUID
    ) -> SessionRecord | None:
        with self._org_transaction(organization_id) as session:
            row = session.scalars(
                select(SessionRow).where(
                    SessionRow.organization_id == organization_id,
                    SessionRow.id == session_id,
                )
            ).one_or_none()
            return None if row is None else _session_record(row)

    def rotate_refresh_token_hash(
        self,
        organization_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        expected_refresh_token_hash: str,
        refresh_token_hash: str,
        now: datetime,
    ) -> bool:
        """Atomically rotate only the current hash of an active session.

        The compare-and-swap closes the concurrent-refresh race: at most one
        presenter can replace a given hash. A loser is therefore refresh-token
        reuse and the service revokes the session family (INV-SESSION-3).
        """
        with self._org_transaction(organization_id) as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SessionRow)
                    .where(
                        SessionRow.organization_id == organization_id,
                        SessionRow.id == session_id,
                        SessionRow.state == _SESSION_STATE_ACTIVE,
                        SessionRow.refresh_token_hash == expected_refresh_token_hash,
                    )
                    .values(
                        refresh_token_hash=refresh_token_hash,
                        last_used_at=now,
                        updated_at=now,
                    )
                ),
            )
            return result.rowcount == 1

    def revoke_session(
        self, organization_id: uuid.UUID, session_id: uuid.UUID, *, now: datetime
    ) -> None:
        with self._org_transaction(organization_id) as session:
            session.execute(
                update(SessionRow)
                .where(
                    SessionRow.organization_id == organization_id,
                    SessionRow.id == session_id,
                    SessionRow.state == _SESSION_STATE_ACTIVE,
                )
                .values(state=_SESSION_STATE_REVOKED, revoked_at=now, updated_at=now)
            )

    def revoke_all_sessions(
        self, organization_id: uuid.UUID, auth_identity_id: uuid.UUID, *, now: datetime
    ) -> int:
        """Revoke every active session of one identity (INV-SESSION-2)."""
        with self._org_transaction(organization_id) as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SessionRow)
                    .where(
                        SessionRow.organization_id == organization_id,
                        SessionRow.auth_identity_id == auth_identity_id,
                        SessionRow.state == _SESSION_STATE_ACTIVE,
                    )
                    .values(state=_SESSION_STATE_REVOKED, revoked_at=now, updated_at=now)
                ),
            )
            return result.rowcount
