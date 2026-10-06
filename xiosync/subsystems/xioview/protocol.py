"""XIOVIEW Protocol — shared types, constants, and interfaces.

Central definitions used across all XIOVIEW modules. Import from here
to avoid circular dependencies between session_manager, control,
screencast, and routes.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# ── Observation mode identifiers ──────────────────────────────────────────────

MODE_SCREENSHOT = "screenshot"
MODE_CDP_SCREENCAST = "cdp_screencast"
MODE_DOM_STREAM = "dom_stream"
MODE_CDP_DOM_SNAPSHOT = "cdp_dom_snapshot"
MODE_DOM_OVERLAY = "dom_overlay"

# ── Server → Client message types ─────────────────────────────────────────────

MSG_FRAME = "frame"
MSG_CONNECTED = "connected"
MSG_SESSION_INFO = "session_info"
MSG_INTERACTION_ACK = "interaction_ack"
MSG_INTERACTION_BLOCKED = "interaction_blocked"
MSG_DOM_EVENT = "dom_event"
MSG_DOM_SNAPSHOT = "dom_snapshot"
MSG_DOM_OVERLAY = "dom_overlay"
MSG_ACTION_LOG = "action_log"
MSG_KEEPALIVE = "keepalive"
MSG_ERROR = "error"

# ── Client → Server control message types ─────────────────────────────────────

CTL_MOUSE_MOVE = "mouse_move"
CTL_MOUSEDOWN = "mousedown"
CTL_MOUSEUP = "mouseup"
CTL_CLICK = "click"
CTL_DBLCLICK = "dblclick"
CTL_KEY = "key"
CTL_TYPE = "type"
CTL_SCROLL = "scroll"
CTL_PAUSE_WORKFLOW = "pause_workflow"
CTL_RESUME_WORKFLOW = "resume_workflow"
CTL_SET_FPS = "set_fps"

# Control types that require a live page object
PAGE_CONTROL_TYPES = frozenset({
    CTL_MOUSE_MOVE, CTL_MOUSEDOWN, CTL_MOUSEUP, CTL_CLICK,
    CTL_DBLCLICK, CTL_KEY, CTL_TYPE, CTL_SCROLL,
})

# Control types blocked while an automated workflow is executing
BLOCKABLE_CONTROL_TYPES = frozenset({
    CTL_CLICK, CTL_MOUSEDOWN, CTL_MOUSEUP, CTL_DBLCLICK,
    CTL_KEY, CTL_TYPE, CTL_SCROLL, CTL_MOUSE_MOVE,
})

# ── DOM Cursor Injection Script ────────────────────────────────────────────────

DOM_CURSOR_JS = """(function(){
  if(document.getElementById('__xio_cur'))return;
  var el=document.createElement('div');
  el.id='__xio_cur';
  /* Use top/left (not transform) — forces raster repaint on SwiftShader/Xvfb
     so the cursor appears in CDP screencasted frames. */
  el.style.cssText='position:fixed;left:0px;top:0px;width:22px;height:22px;pointer-events:none;z-index:2147483647;transition:none;';
  el.innerHTML='<svg width="22" height="22" viewBox="0 0 22 22" xmlns="http://www.w3.org/2000/svg"><filter id="xs"><feDropShadow dx="1" dy="1" stdDeviation="1.2" flood-opacity="0.6"/></filter><path d="M2 2 L2 18 L6 14 L9.5 21 L12 20 L8.5 13 L14 13 Z" fill="#fff" stroke="#000" stroke-width="1" filter="url(#xs)"/></svg>';
  document.documentElement.appendChild(el);
  document.addEventListener('mousemove',function(e){
    el.style.left=e.clientX+'px';
    el.style.top=e.clientY+'px';
  },{passive:true,capture:true});
})();"""


# ── Protocols ──────────────────────────────────────────────────────────────────

@runtime_checkable
class PageLike(Protocol):
    """Minimal interface for a Playwright/Patchright Page object."""

    @property
    def url(self) -> str: ...

    def is_closed(self) -> bool: ...

    async def screenshot(self, **kwargs: Any) -> bytes: ...

    async def evaluate(self, expression: str) -> Any: ...

    @property
    def context(self) -> Any: ...

    @property
    def keyboard(self) -> Any: ...
