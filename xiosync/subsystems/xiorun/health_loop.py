"""health_loop.py — XIORUN worker health-check tick.

Registered in worker/main.py as:
    ("xiorun-health", tick_xiorun_health, 30.0)

Runs every 30s in the worker thread loop. Checks all sessions registered
in XIORunRuntimePool:
  - Detects pages closed unexpectedly (Chromium crash on Colab)
  - Triggers emergency save + marks session 'failed'
  - Logs active browser count and per-session state
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_LAST_RUN: datetime | None = None
_INTERVAL_S: float = 30.0


def tick_xiorun_health(session: Session) -> int:
    """Health-check all live XIORUN browser sessions. Returns count checked."""
    global _LAST_RUN

    now = datetime.now(UTC)
    if _LAST_RUN and (now - _LAST_RUN).total_seconds() < _INTERVAL_S:
        return 0
    _LAST_RUN = now

    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
        import asyncio  # noqa: PLC0415

        pool = get_runtime_pool()
        entries = pool.list_sessions()

        if not entries:
            return 0

        logger.info("xiorun.health.tick", extra={
            "active_sessions": len(entries)
        })

        checked = 0
        for entry in entries:
            sid  = entry["session_id"]
            closed = entry["page_closed"]
            checked += 1

            if closed:
                logger.error("xiorun.health.page_closed_unexpectedly", extra={
                    "session_id": sid,
                })
                launcher = pool.get_launcher(sid)
                if launcher:
                    # Schedule emergency save onto the running event loop from this
                    # sync worker thread. asyncio.create_task() is invalid here.
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                launcher._emergency_save(), loop
                            )
                    except Exception:
                        pass

                # Mark failed in DB (sync — we're in a worker thread)
                try:
                    from sqlalchemy import text  # noqa: PLC0415
                    session.execute(
                        text("""
                            UPDATE browser_sessions
                            SET state = 'failed', updated_at = now()
                            WHERE id = :sid
                        """),
                        {"sid": sid},
                    )
                    session.commit()
                except Exception as exc:
                    logger.warning("xiorun.health.mark_failed_error", extra={
                        "session_id": sid, "error": str(exc)
                    })

                # Remove from pool
                pool._unregister(sid)

        return checked

    except Exception as exc:
        logger.warning("xiorun.health.tick_error", extra={"error": str(exc)})
        return 0
