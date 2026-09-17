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
    project_id: uuid.UUID | None
    pool_id: uuid.UUID
    node_id: uuid.UUID | None
    session_data: dict[str, Any]
    state: str
    created_at: datetime
    # PPPoE exit node identity (None when no residential IP assigned)
    pppoe_exit_node_id: uuid.UUID | None = None
    pppoe_host_id: uuid.UUID | None = None
    pppoe_slot: int | None = None
    proxy_url: str | None = None
    public_ip: str | None = None
    worker_ts_ip: str | None = None


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
        project_id=row.project_id,
        pool_id=row.pool_id,
        node_id=row.node_id,
        session_data=dict(row.session_data),
        state=row.state,
        created_at=row.created_at,
        pppoe_exit_node_id=getattr(row, "pppoe_exit_node_id", None),
        pppoe_host_id=getattr(row, "pppoe_host_id", None),
        pppoe_slot=getattr(row, "pppoe_slot", None),
        proxy_url=getattr(row, "proxy_url", None),
        public_ip=getattr(row, "public_ip", None),
        worker_ts_ip=getattr(row, "worker_ts_ip", None),
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
        from xiosync.persistence.models.browser import BrowserPool
        pool = self._session.scalar(
            select(BrowserPool).where(
                BrowserPool.id == pool_id,
                BrowserPool.organization_id == context.organization_id
            )
        )
        if not pool: raise ValueError(f"Browser pool {pool_id} not found")

        # Attempt to acquire a residential exit node from the warm pool.
        # Failures are non-fatal — session is created without a proxy in that case.
        pppoe_slot = None
        try:
            from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
            pppoe_svc = PPPoENodeService(self._session)
            worker_ts_ip = (config or {}).get("worker_ts_ip", "")
            if worker_ts_ip:
                pppoe_slot = pppoe_svc.acquire_any(
                    context, session_id="pending", worker_ts_ip=worker_ts_ip
                )
        except Exception as _pppoe_err:
            import logging
            logging.getLogger(__name__).warning(
                "PPPoE acquire failed — session will have no residential IP: %s", _pppoe_err
            )

        session_row = BrowserSession(
            id=new_id(),
            organization_id=context.organization_id,
            project_id=pool.project_id,
            pool_id=pool_id,
            session_data=dict(config) if config else {},
            state="initializing",
            created_at=now,
            pppoe_exit_node_id=uuid.UUID(str(pppoe_slot.id)) if pppoe_slot else None,
            pppoe_host_id=uuid.UUID(str(pppoe_slot.host_id)) if pppoe_slot else None,
            pppoe_slot=pppoe_slot.ppp_slot if pppoe_slot else None,
            proxy_url=pppoe_slot.proxy_url if pppoe_slot else None,
            public_ip=pppoe_slot.public_ip if pppoe_slot else None,
            worker_ts_ip=(config or {}).get("worker_ts_ip") if pppoe_slot else None,
        )
        self._session.add(session_row)

        # Now that we have the real session ID, backfill it on the PPPoE node record
        if pppoe_slot:
            try:
                from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
                from sqlalchemy import text as _text
                self._session.execute(
                    _text("UPDATE xiogrid_pppoe_exit_nodes SET assigned_session_id = :sid WHERE id = :nid"),
                    {"sid": str(session_row.id), "nid": str(pppoe_slot.id)},
                )
            except Exception:
                pass

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

    def set_state(
        self,
        session_id: uuid.UUID,
        state: str,
        *,
        worker_ts_ip: str | None = None,
    ) -> None:
        """Update browser_sessions.state (and optionally worker_ts_ip).

        Called by XIORUN (launcher.py) at each lifecycle transition:
          initializing → active  (browser launched and CDP attached)
          active → suspended     (graceful teardown, profile saved)
          active → failed        (proxy lost, Chromium crashed)

        Does NOT require OrgContext — XIORUN operates at the platform level
        and already holds a validated session_id from _claim_next_pending().

        Args:
            session_id:   UUID of the browser_sessions row to update.
            state:        Target state: 'initializing'|'active'|'suspended'|
                          'terminated'|'failed'.
            worker_ts_ip: Tailscale IP of the Colab node — set on first
                          transition to 'active', None otherwise.
        """
        _VALID_STATES = {"initializing", "active", "suspended", "terminated", "failed"}
        if state not in _VALID_STATES:
            raise ValueError(f"Invalid browser session state: {state!r}")

        now = datetime.now(UTC)
        values: dict[str, Any] = {"state": state, "updated_at": now}
        if worker_ts_ip is not None:
            values["worker_ts_ip"] = worker_ts_ip

        self._session.execute(
            update(BrowserSession)
            .where(BrowserSession.id == session_id)
            .values(**values)
        )
        # No flush/commit — caller owns the transaction boundary

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

        # ── Signal 1: ORM state ───────────────────────────────────────────────
        # Terminal states map directly to health status
        if row.state in ("terminated", "failed"):
            status = "dead"
        elif row.state == "suspended":
            status = "soon"
        elif row.state == "initializing":
            # Initializing for more than 3 minutes → stale init, treat as immediate
            age_secs = (now - row.created_at).total_seconds() if row.created_at else 0
            status = "immediate" if age_secs > 180 else "healthy"
        else:
            # state == "active"
            # ── Signal 2: Staleness via updated_at ───────────────────────────
            # If the active session hasn't been touched in > 10 minutes → stale
            last_touch = row.updated_at or row.created_at
            idle_secs = (now - last_touch).total_seconds() if last_touch else 0
            if idle_secs > 600:    # 10 minutes — session is likely zombie
                status = "immediate"
            elif idle_secs > 300:  # 5 minutes — worth checking
                status = "soon"
            else:
                status = "healthy"

            # ── Signal 3: TCP CDP port probe (non-blocking, best-effort) ─────
            # Attempt a 1-second TCP connect to the CDP port via Tailscale IP.
            # If it fails we escalate the status by one level (healthy→soon,
            # soon→immediate) but never downgrade a terminal-state verdict.
            ts_ip = getattr(row, "worker_ts_ip", None)
            cdp_port = (row.session_data or {}).get("port")
            if ts_ip and cdp_port and status != "dead":
                import socket as _sock  # noqa: PLC0415
                try:
                    with _sock.create_connection((ts_ip, int(cdp_port)), timeout=1.0):
                        pass  # CDP port reachable — status unchanged
                except OSError:
                    # Port unreachable — escalate
                    if status == "healthy":
                        status = "soon"
                    elif status == "soon":
                        status = "immediate"

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

        # Release the PPPoE exit node back to the idle pool (best-effort)
        if row.pppoe_host_id and row.pppoe_slot is not None:
            try:
                from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
                PPPoENodeService(self._session).release_from_worker(
                    row.pppoe_host_id, row.pppoe_slot
                )
            except Exception as _rel_err:
                import logging
                logging.getLogger(__name__).warning(
                    "PPPoE release failed on session termination %s: %s", session_id, _rel_err
                )

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
        project_id: uuid.UUID | None = None,
        pool_id: uuid.UUID | None = None,
        state: str | None = None,
    ) -> list[BrowserSessionRecord]:
        """List sessions in the org, filtered by project_id, pool_id and/or state."""
        stmt = (
            select(BrowserSession)
            .where(BrowserSession.organization_id == context.organization_id)
            .order_by(BrowserSession.created_at.desc())
        )
        if project_id is not None:
            stmt = stmt.where(BrowserSession.project_id == project_id)
        if pool_id is not None:
            stmt = stmt.where(BrowserSession.pool_id == pool_id)
        if state is not None:
            stmt = stmt.where(BrowserSession.state == state)

        rows = self._session.scalars(stmt).all()
        return [_record(row) for row in rows]
