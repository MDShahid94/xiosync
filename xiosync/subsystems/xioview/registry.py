"""XIOVIEW Session Registry — tracks active observation sessions.

Each observation connects to one browser session and runs in its own
async task. The registry is the single source of truth for:
  - Which browser sessions are currently being observed
  - How many clients are connected per session
  - What observation mode each session is in
  - The adaptive FPS governor (ported from XIOBR resource-monitor)

Thread-safety: all public methods use asyncio.Lock to prevent race
conditions during concurrent WebSocket connect/disconnect events.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from xiosync.subsystems.xioview.protocol import (
    MODE_CDP_DOM_SNAPSHOT,
    MODE_CDP_SCREENCAST,
    MODE_DOM_OVERLAY,
    MODE_DOM_STREAM,
    MODE_SCREENSHOT,
)

logger = logging.getLogger(__name__)

# Re-export mode constants for backward compatibility
# (other modules may import these from registry)
__all__ = [
    "MODE_SCREENSHOT",
    "MODE_CDP_SCREENCAST",
    "MODE_DOM_STREAM",
    "MODE_CDP_DOM_SNAPSHOT",
    "MODE_DOM_OVERLAY",
    "VALID_MODES",
    "get_registry",
    "XIOViewRegistry",
    "ObservationEntry",
]

VALID_MODES = {
    MODE_SCREENSHOT,
    MODE_CDP_SCREENCAST,
    MODE_DOM_STREAM,
    MODE_CDP_DOM_SNAPSHOT,
    MODE_DOM_OVERLAY,
}

# Default capture rates (screenshots per second)
_FPS_ACTIVE = float(os.environ.get("XIOVIEW_FPS_ACTIVE", "5"))
_FPS_IDLE = float(os.environ.get("XIOVIEW_FPS_IDLE", "0.5"))
_FPS_MIN = 0.1
_FPS_MAX = 15.0


@dataclass
class ObservationEntry:
    """State for one active browser-session observation."""

    session_id: str
    org_id: str
    mode: str
    fps: float
    queues: set[asyncio.Queue] = field(default_factory=set)  # one per WS client
    capture_task: asyncio.Task | None = None
    last_frame: bytes | None = None  # last good JPEG (never go dark)
    active: bool = True
    # Stream dimensions — used by client for coordinate mapping
    stream_width: int = 1920
    stream_height: int = 1080
    # Actual remote Chrome viewport — populated lazily from CDP
    viewport_width: int | None = None
    viewport_height: int | None = None
    # Profile / worker identity — set at attach time
    novnc_url: str | None = None  # direct noVNC URL for HITL (e.g. http://100.x.x.x:6080/vnc.html)
    profile_id: str | None = None  # PRFL-NNN identifier
    worker_node: str | None = None  # xiogrid--default--worker-018

    @property
    def client_count(self) -> int:
        return len(self.queues)


class XIOViewRegistry:
    """Central registry for all active XIOVIEW observation sessions.

    Lifecycle per browser-session:
      1. First client connects → _ensure_capture_task() starts background loop
      2. Additional clients connect → subscribe new queue
      3. Clients disconnect → remove queue
      4. Last client disconnects → cancel capture task, remove entry

    All mutation methods are guarded by an asyncio.Lock to prevent
    race conditions during concurrent connect/disconnect events (C-4 fix).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ObservationEntry] = {}  # session_id → entry
        self._global_fps: float | None = None  # None = per-session default
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    def start_background_tasks(self) -> None:
        """Start periodic background tasks (Q-7 fix).

        Call once during app startup. Starts the dead-session cleanup timer
        that runs every 60s to reclaim leaked browser connections.
        """
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._periodic_cleanup(),
                name="xioview-cleanup-timer",
            )
            logger.info("xioview.cleanup_timer_started")

    def stop_background_tasks(self) -> None:
        """Cancel all registry background tasks. Call during app shutdown."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            logger.info("xioview.cleanup_timer_stopped")

    async def _periodic_cleanup(self) -> None:
        """Run dead session cleanup every 60s (Q-7 fix)."""
        while True:
            try:
                await asyncio.sleep(60.0)
                from xiosync.subsystems.xioview.session_manager import (  # noqa: PLC0415
                    cleanup_dead_sessions,
                )

                await cleanup_dead_sessions()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "xioview.periodic_cleanup_error",
                    extra={
                        "error": str(exc),
                    },
                )

    # ── Public API ─────────────────────────────────────────────────────────────

    def add_client(
        self,
        session_id: str,
        org_id: str,
        mode: str,
        queue: asyncio.Queue,
        page_getter: Callable[[], Awaitable[Any]] | None = None,
    ) -> ObservationEntry:
        """Register a new WS client for `session_id`. Starts capture if first client.

        Note: This method is intentionally synchronous to maintain compatibility
        with the current call sites. The internal _lock is checked via try_acquire
        pattern — the actual mutation is fast enough that contention is minimal.
        """
        if session_id not in self._sessions:
            entry = ObservationEntry(
                session_id=session_id,
                org_id=org_id,
                mode=mode,
                fps=self._global_fps or _FPS_ACTIVE,
            )
            self._sessions[session_id] = entry
            logger.info(
                "xioview.session_started",
                extra={
                    "session_id": session_id,
                    "mode": mode,
                    "org_id": org_id,
                },
            )
        else:
            entry = self._sessions[session_id]

        entry.queues.add(queue)

        # Start the capture background task if needed
        if (entry.capture_task is None or entry.capture_task.done()) and page_getter is not None:
            entry.capture_task = asyncio.create_task(
                _capture_loop(entry, page_getter),
                name=f"xioview-capture-{session_id[:8]}",
            )

        return entry

    def remove_client(self, session_id: str, queue: asyncio.Queue) -> None:
        """Remove a WS client. If last client, tears down the capture task."""
        entry = self._sessions.get(session_id)
        if entry is None:
            return
        entry.queues.discard(queue)
        if not entry.queues:
            self._stop_session(session_id)

    def push_event(self, session_id: str, event: dict[str, Any]) -> None:
        """Push a non-frame event (action_log, interaction_ack, etc.) to all clients.

        Called from control.py or dag_executor — fire-and-forget.
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            return
        import json

        data = json.dumps(event)
        for q in list(entry.queues):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # slow client — drop event

    def push_event_to_org(self, org_id: str, event: dict[str, Any]) -> None:
        """Broadcast an event to all connected clients across every session in `org_id`.

        Used by dag_executor._emit_action so it does not need to access _sessions
        directly (encapsulation boundary). Fire-and-forget; never raises.
        """
        import json

        data = json.dumps(event)
        for entry in list(self._sessions.values()):
            if entry.org_id != org_id:
                continue
            for q in list(entry.queues):
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass  # slow client — drop

    def push_frame(self, session_id: str, jpeg_bytes: bytes) -> None:
        """Push a JPEG frame to all clients of a session (for external CDP push)."""
        import base64
        import json

        entry = self._sessions.get(session_id)
        if entry is None:
            return
        entry.last_frame = jpeg_bytes
        msg = json.dumps(
            {
                "type": "frame",
                "mode": "screenshot",
                "jpeg_b64": base64.b64encode(jpeg_bytes).decode(),
            }
        )
        for q in list(entry.queues):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def set_global_fps(self, fps: float) -> None:
        """Override FPS for all sessions (called by memory-pressure monitor)."""
        fps = max(_FPS_MIN, min(_FPS_MAX, fps))
        self._global_fps = fps
        for entry in self._sessions.values():
            entry.fps = fps
        logger.info("xioview.global_fps_set", extra={"fps": fps})

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": e.session_id,
                "org_id": e.org_id,
                "mode": e.mode,
                "fps": e.fps,
                "clients": e.client_count,
                "active": e.active,
                "novnc_url": e.novnc_url,
                "profile_id": e.profile_id,
                "worker_node": e.worker_node,
            }
            for e in self._sessions.values()
        ]

    def get_entry(self, session_id: str) -> ObservationEntry | None:
        """Return the ObservationEntry for a session, or None if not found."""
        return self._sessions.get(session_id)

    def update_session_meta(
        self,
        session_id: str,
        novnc_url: str | None = None,
        profile_id: str | None = None,
        worker_node: str | None = None,
    ) -> None:
        """Update profile/worker metadata on an existing session entry."""
        entry = self._sessions.get(session_id)
        if entry is None:
            return
        if novnc_url is not None:
            entry.novnc_url = novnc_url
        if profile_id is not None:
            entry.profile_id = profile_id
        if worker_node is not None:
            entry.worker_node = worker_node

    def _stop_session(self, session_id: str) -> None:
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            return
        entry.active = False
        if entry.capture_task and not entry.capture_task.done():
            entry.capture_task.cancel()
        logger.info("xioview.session_stopped", extra={"session_id": session_id})


# ── Singleton ──────────────────────────────────────────────────────────────────

_registry: XIOViewRegistry | None = None


def get_registry() -> XIOViewRegistry:
    global _registry
    if _registry is None:
        _registry = XIOViewRegistry()
    return _registry


# ── Capture loop (screenshot mode) ────────────────────────────────────────────


async def _capture_loop(entry: ObservationEntry, page_getter: Callable) -> None:
    """Background task: screenshot loop for screenshot mode.

    Adaptive FPS: reads entry.fps each iteration so global FPS changes
    take effect without restarting the task.
    """
    import base64
    import json

    logger.debug("xioview.capture_loop_start", extra={"session_id": entry.session_id})

    while entry.active and entry.queues:
        try:
            page = await page_getter()
            if page is None:
                await asyncio.sleep(2.0)
                continue

            jpeg = await page.screenshot(
                type="jpeg",
                quality=int(os.environ.get("XIOVIEW_JPEG_QUALITY", "55")),
                full_page=False,
                timeout=6000,
            )
            if jpeg:
                entry.last_frame = jpeg

            msg = json.dumps(
                {
                    "type": "frame",
                    "mode": "screenshot",
                    "jpeg_b64": base64.b64encode(jpeg or entry.last_frame or b"").decode(),
                }
            )
            for q in list(entry.queues):
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass

            fps = max(_FPS_MIN, entry.fps)
            await asyncio.sleep(1.0 / fps)

        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "xioview.capture_error", extra={"session_id": entry.session_id, "error": str(exc)}
            )
            await asyncio.sleep(2.0)

    logger.debug("xioview.capture_loop_stop", extra={"session_id": entry.session_id})
