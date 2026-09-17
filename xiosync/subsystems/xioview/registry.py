"""XIOVIEW Session Registry — tracks active observation sessions.

Each observation connects to one browser session and runs in its own
async task. The registry is the single source of truth for:
  - Which browser sessions are currently being observed
  - How many clients are connected per session
  - What observation mode each session is in
  - The adaptive FPS governor (ported from XIOBR resource-monitor)

Thread-safety: all public methods are called from async coroutines in
the same event loop. No external locking needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# Observation modes
MODE_SCREENSHOT = "screenshot"
MODE_CDP_SCREENCAST = "cdp_screencast"
MODE_DOM_STREAM = "dom_stream"

VALID_MODES = {MODE_SCREENSHOT, MODE_CDP_SCREENCAST, MODE_DOM_STREAM}

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
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ObservationEntry] = {}   # session_id → entry
        self._global_fps: float | None = None               # None = per-session default

    # ── Public API ─────────────────────────────────────────────────────────────

    def add_client(
        self,
        session_id: str,
        org_id: str,
        mode: str,
        queue: asyncio.Queue,
        page_getter: Callable[[], Awaitable[Any]] | None = None,
    ) -> ObservationEntry:
        """Register a new WS client for `session_id`. Starts capture if first client."""
        if session_id not in self._sessions:
            entry = ObservationEntry(
                session_id=session_id,
                org_id=org_id,
                mode=mode,
                fps=self._global_fps or _FPS_ACTIVE,
            )
            self._sessions[session_id] = entry
            logger.info("xioview.session_started", extra={
                "session_id": session_id, "mode": mode, "org_id": org_id,
            })
        else:
            entry = self._sessions[session_id]

        entry.queues.add(queue)

        # Start the capture background task if needed
        if (
            entry.capture_task is None or entry.capture_task.done()
        ) and page_getter is not None:
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
        """Push a non-frame event (action_log, dom_event, etc.) to all clients.

        Called from dag_executor or rrweb bridge — fire-and-forget.
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
                pass  # slow client — drop frame

    def push_event_to_org(self, org_id: str, event: dict[str, Any]) -> None:
        """Broadcast an event to all connected clients across every session in `org_id`.

        Used by dag_executor._emit_action so it does not need to access _sessions
        directly (encapsulation boundary).  Fire-and-forget; never raises.
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
        msg = json.dumps({"type": "frame", "mode": "screenshot",
                          "jpeg_b64": base64.b64encode(jpeg_bytes).decode()})
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
            }
            for e in self._sessions.values()
        ]

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
    Ported from XIOBR screencaster.mjs capture pattern.
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

            msg = json.dumps({
                "type": "frame",
                "mode": "screenshot",
                "jpeg_b64": base64.b64encode(jpeg or entry.last_frame or b"").decode(),
            })
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
            logger.warning("xioview.capture_error", extra={
                "session_id": entry.session_id, "error": str(exc)
            })
            await asyncio.sleep(2.0)

    logger.debug("xioview.capture_loop_stop", extra={"session_id": entry.session_id})
