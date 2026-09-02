"""Browser session management service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.browser import BrowserSession
from xiosync.persistence.models.operations import Operation
from xiosync.platform.ids import new_id

__all__ = [
    "BrowserSessionNotFoundError",
    "BrowserSessionRecord",
    "BrowserSessionService",
    "SessionHealthRecord",
]


@dataclass(frozen=True, slots=True)
class BrowserSessionRecord:
    """Frozen snapshot of a browser_sessions row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    pool_id: uuid.UUID
    node_id: uuid.UUID | None
    session_data: dict[str, Any]
    state: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionHealthRecord:
    """Health check status for a browser session."""

    session_id: uuid.UUID
    status: str
    checked_at: datetime


class BrowserSessionNotFoundError(ValueError):
    """Raised when the requested browser session does not exist in the org."""

    def __init__(self, session_id: uuid.UUID) -> None:
        super().__init__(f"Browser session {session_id} not found")


def _record(row: BrowserSession) -> BrowserSessionRecord:
    return BrowserSessionRecord(
        id=row.id,
        organization_id=row.organization_id,
        pool_id=row.pool_id,
        node_id=row.node_id,
        session_data=dict(row.session_data),
        state=row.state,
        created_at=row.created_at,
    )


class BrowserSessionService:
    """Enterprise browser session management service."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_session(
        self,
        context: OrgContext,
        *,
        pool_id: uuid.UUID,
        config: Mapping[str, Any] | None = None,
    ) -> BrowserSessionRecord:
        """Create a new browser session within a pool and record an audit operation."""
        now = datetime.now(UTC)
        session_row = BrowserSession(
            id=new_id(),
            organization_id=context.organization_id,
            pool_id=pool_id,
            session_data=dict(config) if config else {},
            state="initializing",
            created_at=now,
        )
        self._session.add(session_row)

        op_id = new_id()
        self._session.add(
            Operation(
                id=op_id,
                organization_id=context.organization_id,
                actor_id=context.actor_id,
                operation="browser.session.create",
                trigger="user_command",
                initiated_by=context.actor_id,
                scope="organization",
                outcome="success",
                rationale=f"Created browser session for pool: {pool_id}",
                started_at=now,
                completed_at=now,
            )
        )
        self._session.add(
            Event(
                id=new_id(),
                organization_id=context.organization_id,
                event_type="browser_session.created",
                actor_id=context.actor_id,
                severity="info",
                operation_id=op_id,
                entity_type="browser_session",
                entity_id=session_row.id,
                payload={"pool_id": str(pool_id)},
                created_at=now,
            )
        )
        self._session.flush()
        return _record(session_row)

    def verify_session(
        self,
        context: OrgContext,
        session_id: uuid.UUID,
    ) -> SessionHealthRecord:
        """Verify the health of a session (e.g. cookie expiration)."""
        row = self._session.scalar(
            select(BrowserSession).where(
                BrowserSession.id == session_id,
                BrowserSession.organization_id == context.organization_id,
            )
        )
        if row is None:
            raise BrowserSessionNotFoundError(session_id)
        
        now = datetime.now(UTC)
        # Dummy health check logic mapped to the states in decoupling plan
        # In actual implementation, we would inspect cookies, indexeddb, etc.
        # Defaulting to healthy for the stub.
        status = "healthy"
        if row.state == "failed":
            status = "immediate"
        elif row.state == "suspended":
            status = "soon"

        op_id = new_id()
        self._session.add(
            Operation(
                id=op_id,
                organization_id=context.organization_id,
                actor_id=context.actor_id,
                operation="browser.session.verify",
                trigger="system_check",
                initiated_by=context.actor_id,
                scope="organization",
                outcome="success",
                rationale=f"Verified session health: {status}",
                started_at=now,
                completed_at=now,
            )
        )
        self._session.add(
            Event(
                id=new_id(),
                organization_id=context.organization_id,
                event_type="browser_session.verified",
                actor_id=context.actor_id,
                severity="info",
                operation_id=op_id,
                entity_type="browser_session",
                entity_id=session_id,
                payload={"status": status},
                created_at=now,
            )
        )
        self._session.flush()

        return SessionHealthRecord(
            session_id=session_id,
            status=status,
            checked_at=now,
        )

    def terminate_session(
        self,
        context: OrgContext,
        session_id: uuid.UUID,
    ) -> None:
        """Terminate an active browser session."""
        row = self._session.scalar(
            select(BrowserSession).where(
                BrowserSession.id == session_id,
                BrowserSession.organization_id == context.organization_id,
            )
        )
        if row is None:
            raise BrowserSessionNotFoundError(session_id)

        now = datetime.now(UTC)
        old_state = row.state

        self._session.execute(
            update(BrowserSession)
            .where(BrowserSession.id == session_id)
            .values(state="terminated", updated_at=now)
        )

        op_id = new_id()
        self._session.add(
            Operation(
                id=op_id,
                organization_id=context.organization_id,
                actor_id=context.actor_id,
                operation="browser.session.terminate",
                trigger="user_command",
                initiated_by=context.actor_id,
                scope="organization",
                outcome="success",
                from_state=old_state,
                to_state="terminated",
                rationale="Terminated browser session",
                started_at=now,
                completed_at=now,
            )
        )
        self._session.add(
            Event(
                id=new_id(),
                organization_id=context.organization_id,
                event_type="browser_session.terminated",
                actor_id=context.actor_id,
                severity="info",
                operation_id=op_id,
                entity_type="browser_session",
                entity_id=session_id,
                payload={},
                created_at=now,
            )
        )
        self._session.flush()

    def list_sessions(
        self,
        context: OrgContext,
        *,
        pool_id: uuid.UUID | None = None,
        state: str | None = None,
    ) -> list[BrowserSessionRecord]:
        """List sessions in the org, filtered by pool_id and/or state."""
        stmt = (
            select(BrowserSession)
            .where(BrowserSession.organization_id == context.organization_id)
            .order_by(BrowserSession.created_at.desc())
        )
        if pool_id is not None:
            stmt = stmt.where(BrowserSession.pool_id == pool_id)
        if state is not None:
            stmt = stmt.where(BrowserSession.state == state)

        rows = self._session.scalars(stmt).all()
        return [_record(row) for row in rows]
