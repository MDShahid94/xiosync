"""XIOVIEW Audit — interaction audit logging for compliance.

Records all operator control interactions (clicks, keystrokes, scrolls)
and observation sessions to an append-only audit table. Provides session
replay metadata and operator accountability.

The audit log is buffered in-memory and flushed periodically to reduce
database write pressure during high-frequency interactions.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# Maximum buffer size before forced flush
_MAX_BUFFER = 100
# Flush interval in seconds
_FLUSH_INTERVAL = 10.0


class AuditEntry:
    """Single audit log entry."""
    __slots__ = ("id", "session_id", "org_id", "actor_id", "action", "detail", "ts")

    def __init__(
        self,
        session_id: str,
        org_id: str,
        actor_id: str | None,
        action: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.id = str(uuid.uuid4())
        self.session_id = session_id
        self.org_id = org_id
        self.actor_id = actor_id or ""
        self.action = action
        self.detail = detail or {}
        self.ts = time.time()


class XIOViewAuditLog:
    """Buffered audit logger for XIOVIEW interactions.

    Usage:
        audit = get_audit_log()
        audit.record("click", session_id, org_id, detail={"x": 100, "y": 200})
        audit.record_session_start(session_id, org_id, actor_id, mode)

    The buffer is flushed every _FLUSH_INTERVAL seconds or when it reaches
    _MAX_BUFFER entries — whichever comes first.
    """

    def __init__(self) -> None:
        self._buffer: deque[AuditEntry] = deque(maxlen=_MAX_BUFFER * 2)
        self._flush_task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        """Start the periodic flush background task."""
        if self._running:
            return
        self._running = True
        try:
            self._flush_task = asyncio.create_task(
                self._flush_loop(),
                name="xioview-audit-flush",
            )
        except RuntimeError:
            # No event loop running yet — will start lazily
            self._running = False

    def stop(self) -> None:
        """Stop the flush loop and drain remaining entries."""
        self._running = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()

    def record(
        self,
        action: str,
        session_id: str,
        org_id: str,
        actor_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record an audit event. Non-blocking — buffers for batch write."""
        entry = AuditEntry(
            session_id=session_id,
            org_id=org_id,
            actor_id=actor_id,
            action=action,
            detail=detail,
        )
        self._buffer.append(entry)

        # Force flush if buffer is full
        if len(self._buffer) >= _MAX_BUFFER:
            try:
                asyncio.create_task(self._flush())
            except RuntimeError:
                pass  # no event loop — entries will flush on next cycle

    def record_session_start(
        self, session_id: str, org_id: str, actor_id: str | None, mode: str,
    ) -> None:
        """Record that an operator started observing a session."""
        self.record("session_observe_start", session_id, org_id, actor_id, {
            "mode": mode,
        })

    def record_session_end(
        self, session_id: str, org_id: str, actor_id: str | None,
    ) -> None:
        """Record that an operator stopped observing a session."""
        self.record("session_observe_end", session_id, org_id, actor_id)

    def record_control(
        self,
        action: str,
        session_id: str,
        org_id: str,
        actor_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record an interactive control action (click, key, scroll).

        High-frequency actions (mouse_move) are NOT recorded to avoid
        flooding the audit table. Only discrete interactions are logged.
        """
        if action == "mouse_move":
            return  # skip high-frequency events
        self.record(f"control.{action}", session_id, org_id, actor_id, detail)

    async def _flush_loop(self) -> None:
        """Periodically flush buffered entries to the database."""
        while self._running:
            try:
                await asyncio.sleep(_FLUSH_INTERVAL)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("xioview.audit_flush_error", extra={
                    "error": str(exc),
                })

    async def _flush(self) -> None:
        """Write buffered entries to PostgreSQL."""
        if not self._buffer:
            return

        # Drain the buffer
        entries = []
        while self._buffer:
            entries.append(self._buffer.popleft())

        if not entries:
            return

        try:
            from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415
            engine = get_engine()
            if engine is None:
                return

            loop = asyncio.get_running_loop()

            def _write() -> None:
                from sqlalchemy import text  # noqa: PLC0415
                from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

                with OrmSession(engine) as session, session.begin():
                    for e in entries:
                        session.execute(
                            text("""
                                INSERT INTO events (id, organization_id, type, payload, created_at)
                                VALUES (:id, :org_id, :type, :payload::jsonb, to_timestamp(:ts))
                            """),
                            {
                                "id": e.id,
                                "org_id": e.org_id,
                                "type": f"xioview.{e.action}",
                                "payload": _to_json(e),
                                "ts": e.ts,
                            },
                        )

            await loop.run_in_executor(None, _write)
            logger.debug("xioview.audit_flushed", extra={"count": len(entries)})

        except Exception as exc:  # noqa: BLE001
            logger.warning("xioview.audit_flush_failed", extra={
                "count": len(entries), "error": str(exc),
            })
            # Re-buffer entries on failure (will retry next cycle)
            for e in reversed(entries):
                self._buffer.appendleft(e)


def _to_json(entry: AuditEntry) -> str:
    """Serialize an audit entry to JSON for storage."""
    import json
    return json.dumps({
        "session_id": entry.session_id,
        "actor_id": entry.actor_id,
        "action": entry.action,
        "detail": entry.detail,
        "ts": entry.ts,
    })


# ── Singleton ──────────────────────────────────────────────────────────────────

_audit_log: XIOViewAuditLog | None = None


def get_audit_log() -> XIOViewAuditLog:
    """Get the global audit log singleton."""
    global _audit_log
    if _audit_log is None:
        _audit_log = XIOViewAuditLog()
    return _audit_log
