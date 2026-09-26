"""XIOVIEW WebSocket API — observe and control any active browser session.

Endpoint:
  WS /api/v1/xioview/sessions/{session_id}/observe?mode=screenshot|cdp_screencast|dom_stream

The WebSocket carries two inbound streams:
  1. Visual frames  — JPEG base64 (screenshot/CDP) or rrweb DOM events
  2. Action logs    — structured per-node events from dag_executor

And accepts remote-control commands from the client:
  mouse_move, click, key, type, scroll, pause_workflow, resume_workflow

Also provides:
  GET  /xioview/sessions          — list all currently observed sessions
  POST /xioview/sessions/{id}/fps — set adaptive FPS for a session
  POST /xioview/attach            — attach XIOSYNC to a manually-launched CDP session
  GET  /xioview/sessions/{id}/view — serve the browser viewer HTML page
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from xiosync.subsystems.xioview.registry import (
    get_registry, VALID_MODES, MODE_SCREENSHOT, MODE_CDP_SCREENCAST, MODE_DOM_STREAM,
    MODE_CDP_DOM_SNAPSHOT, MODE_DOM_OVERLAY,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioview", tags=["XIOVIEW"])

# Public router — endpoints that DON'T need a JWT (attach uses internal-secret,
# viewer HTML is fully public). Mounted in app.py WITHOUT require_capability.
public_router = APIRouter(prefix="/xioview", tags=["XIOVIEW-public"])

# ── Manually-attached CDP sessions (outside normal BrowserLauncher flow) ───────
# session_id → {pw, browser, context, page}
_attached_browsers: dict[str, dict[str, Any]] = {}

# ── CDP session cache for interactions (one per session, reused) ──────────────
# session_id → CDPSession (from patchright)
_interaction_cdp_sessions: dict[str, Any] = {}


async def _get_or_create_cdp_session(session_id: str, page: Any) -> Any:
    """Get or create a cached CDP session for dispatching interactions.

    Reuses the same CDP session across multiple interactions to avoid
    the overhead of opening a new session for every mouse move / click.
    Falls back to creating a new session if the cached one is dead.
    """
    cdp = _interaction_cdp_sessions.get(session_id)
    if cdp is not None:
        try:
            # Quick health check — if this fails, session is dead
            await cdp.send("Runtime.evaluate", {"expression": "1", "returnByValue": True})
            return cdp
        except Exception:
            _interaction_cdp_sessions.pop(session_id, None)

    cdp = await page.context.new_cdp_session(page)
    _interaction_cdp_sessions[session_id] = cdp
    logger.debug("xioview.cdp_session_created", extra={"session_id": session_id})
    return cdp


async def _scale_coords(
    session_id: str, page: Any, raw_x: int, raw_y: int,
) -> tuple[int, int]:
    """Scale viewer coordinates to actual Chrome viewport coordinates.

    The viewer sends coordinates relative to the stream dimensions (e.g.
    1920×1080 for CDP screencast).  The actual Chrome viewport may differ
    (e.g. 1280×720 on Colab with swiftshader).  This function transforms
    client-space coords → Chrome viewport-space coords.
    """
    attached = _attached_browsers.get(session_id, {})
    stream_w = attached.get("stream_width", 1920)
    stream_h = attached.get("stream_height", 1080)

    # Get actual viewport size (cache in _attached_browsers to avoid per-event CDP calls)
    actual_w = attached.get("_viewport_w")
    actual_h = attached.get("_viewport_h")
    if actual_w is None or actual_h is None:
        try:
            vp = await page.evaluate("() => ({ w: window.innerWidth, h: window.innerHeight })")
            actual_w = vp["w"]
            actual_h = vp["h"]
            if session_id in _attached_browsers:
                _attached_browsers[session_id]["_viewport_w"] = actual_w
                _attached_browsers[session_id]["_viewport_h"] = actual_h
        except Exception:
            actual_w = stream_w
            actual_h = stream_h

    scaled_x = int(raw_x * actual_w / stream_w)
    scaled_y = int(raw_y * actual_h / stream_h)
    return max(0, scaled_x), max(0, scaled_y)


def _is_run_active(session_id: str) -> bool:
    """Check if any run (script OR DAG) is currently executing on this session.

    Delegates to runtime_pool.is_run_active() which tracks active runs for
    both execution paradigms. When a run is active, XIOVIEW blocks manual
    interactions to prevent conflicting input from corrupting execution.
    """
    # 1. Check process-local fast-path cache
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
        if get_runtime_pool().is_run_active(session_id):
            return True
    except Exception:
        pass

    # 2. Cross-process authoritative check (fixes multi-process visibility)
    try:
        from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415
        from sqlalchemy import text  # noqa: PLC0415
        engine = get_engine()
        if engine:
            with engine.connect() as conn:
                res = conn.execute(
                    text("SELECT 1 FROM xioflow_runs WHERE context->>'session_id' = :sid AND state = 'RUNNING'"),
                    {"sid": session_id}
                ).fetchone()
                if res:
                    return True
    except Exception:
        pass

    return False


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_org_id(websocket: WebSocket) -> str:
    """Extract org_id from the WebSocket's auth state (same middleware as HTTP)."""
    ctx = getattr(websocket.state, "org_context", None)
    if ctx:
        return str(ctx.organization_id)
    # Auth token in query param for WS (standard pattern)
    return websocket.query_params.get("org_id", "")


def _assert_session_visible(session_id: str, org_id: str, db: OrmSession) -> dict[str, Any]:
    """Verify the session exists, belongs to this org, and is in a live state."""
    row = db.execute(
        text("""
            SELECT bs.id, bs.state, bs.pool_id, bp.engine_type
            FROM   browser_sessions bs
            JOIN   browser_pools    bp ON bp.id = bs.pool_id
            WHERE  bs.id = :id
              AND  bs.organization_id = :org
        """),
        {"id": session_id, "org": org_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="browser_session_not_found")
    # Valid live states: active (ready for use) or initializing (still launching)
    # suspended/terminated/failed are all considered not streamable
    if row.state not in ("active", "initializing"):
        raise HTTPException(
            status_code=409,
            detail=f"session_not_live (state={row.state})",
        )
    return {"engine_type": row.engine_type, "state": row.state}


async def _get_playwright_page(session_id: str) -> Any | None:
    """Resolve the live Playwright page for a browser session.

    Three-registry lookup (priority order):
      1. _attached_browsers      — manually attached via POST /xioview/attach
      2. run_dispatcher._active_pages — page currently mid-run (DAG executing)
      3. XIORunRuntimePool._pages    — page alive between runs (CDP attached,
                                       Chromium warm on Colab, no DAG executing)

    Returns None if the page cannot be found (session ended, worker restarted).
    """
    # Source 1: manually-attached (highest priority — used for test/admin sessions)
    attached = _attached_browsers.get(session_id)
    if attached:
        page = attached.get("page")
        if page and not page.is_closed():
            return page

    # Source 2: active DAG run page
    try:
        from xiosync.worker.run_dispatcher import get_active_page
        page = await get_active_page(session_id)
        if page is not None:
            return page
    except Exception:
        pass

    # Source 3: Fallback — XIORUN runtime pool (CDP-attached, idle between runs)
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool
        return get_runtime_pool().get_page(session_id)
    except Exception:
        return None


# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.get("/sessions", summary="List all actively observed browser sessions")
def list_observed_sessions() -> dict[str, Any]:
    """Returns sessions currently being screenshotted/streamed via XIOVIEW."""
    return {"sessions": get_registry().list_sessions()}


@router.get(
    "/observable",
    summary="List ALL observable browser sessions (live CDP + actively streamed)",
)
def list_observable_sessions() -> dict[str, Any]:
    """Full multi-session grid data for XIOVIEW UI.

    Merges two sources:
      1. XIOVIEW registry  — sessions with an active WS subscriber (streaming now)
      2. XIORUN runtime_pool — all sessions with a live CDP Page (may be idle)

    A session appears as observable if it exists in either source.
    """
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

    # Source 1: XIOVIEW actively streaming
    observed: dict[str, dict] = {
        s["session_id"]: {**s, "streaming": True}
        for s in get_registry().list_sessions()
    }

    # Source 2: XIORUN runtime pool (CDP alive, may not be streaming yet)
    for entry in get_runtime_pool().list_sessions():
        sid = entry["session_id"]
        if sid not in observed:
            observed[sid] = {
                "session_id":  sid,
                "page_closed": entry["page_closed"],
                "streaming":   False,
                "mode":        None,
                "fps":         None,
            }

    sessions = list(observed.values())
    return {
        "sessions":         sessions,
        "total":            len(sessions),
        "streaming_count":  sum(1 for s in sessions if s.get("streaming")),
        "observable_count": sum(1 for s in sessions if not s.get("page_closed")),
    }



@router.post("/sessions/{session_id}/fps", summary="Set adaptive FPS for a session")
def set_session_fps(session_id: str, fps: float = Query(ge=0.1, le=15)) -> dict[str, Any]:
    """Override the observation FPS for a specific session (e.g. to save bandwidth)."""
    reg = get_registry()
    entry = reg._sessions.get(session_id)
    if not entry:
        raise HTTPException(404, detail="session_not_observed")
    entry.fps = fps
    return {"session_id": session_id, "fps": fps}


# ── Attach endpoint — bridge for manually-launched sessions ───────────────────

class AttachRequest(BaseModel):
    cdp_ws_url: str           # ws://tailscale_ip:PORT from xiorun-agent /launch
    session_id: str | None = None   # auto-generated if omitted
    internal_secret: str | None = None  # XIOSYNC_INTERNAL_SECRET for lightweight auth
    # cdp_screencast works with UC Chrome 131 + swiftshader; screenshot mode returns black frames.
    mode: str = "cdp_screencast"   # "screenshot" | "cdp_screencast"


async def _cleanup_dead_sessions() -> None:
    """Remove _attached_browsers entries whose CDP connection has dropped.

    Only removes sessions where patchright reports the browser as disconnected.
    Active sessions (browser.is_connected()==True) are NEVER removed, regardless
    of age — safe for long-running workflow automation sessions.
    """
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
    dead = [
        sid for sid, entry in list(_attached_browsers.items())
        if not entry.get("browser") or not entry["browser"].is_connected()
    ]
    pool = get_runtime_pool()
    for sid in dead:
        entry = _attached_browsers.pop(sid, None)
        pool._unregister(sid)
        if entry:
            try:
                await entry["browser"].close()
            except Exception:
                pass
            try:
                await entry["pw"].stop()
            except Exception:
                pass
        logger.info("xioview.dead_session_cleaned", extra={"session_id": sid})
    if dead:
        logger.info(f"xioview.cleanup: removed {len(dead)} dead session(s), "
                    f"{len(_attached_browsers)} alive")


@public_router.post("/attach", summary="Attach XIOSYNC to an existing Colab Chrome CDP session")
async def attach_session(body: AttachRequest) -> dict[str, Any]:
    """Connect XIOSYNC's patchright to a running Chrome via CDP URL.

    Use this for manually-launched or test sessions before going through the
    full BrowserLauncher flow. After attaching, open:
      GET /api/v1/xioview/sessions/{session_id}/view
    to observe and control the browser.

    Auth: pass XIOSYNC_INTERNAL_SECRET as internal_secret in the request body,
    OR call from inside the Tailscale network (no external exposure).

    mode: "cdp_screencast" (default) — works with UC Chrome + swiftshader (no black frames).
          "screenshot"               — use only for patchright-native sessions.
    """
    # Lightweight auth — internal secret check (JWT not required for this endpoint)
    expected = os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    if expected and body.internal_secret != expected:
        raise HTTPException(403, detail="invalid_internal_secret")

    from patchright.async_api import async_playwright  # noqa: PLC0415
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

    # Clean up dead (disconnected) sessions first — never removes live sessions
    await _cleanup_dead_sessions()

    session_id = body.session_id or str(uuid.uuid4())

    # Close any existing attachment for this session
    old = _attached_browsers.pop(session_id, None)
    if old:
        try:
            await old["browser"].close()
        except Exception:
            pass
        try:
            await old["pw"].stop()
        except Exception:
            pass

    # CDP URL: patchright connect_over_cdp expects http://host:port
    cdp_http_url = body.cdp_ws_url.replace("ws://", "http://").split("/json")[0]
    logger.info("xioview.attach_start", extra={"session_id": session_id, "url": cdp_http_url,
                                                "mode": body.mode})

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp_http_url)
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            context = await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
    except Exception as exc:
        await pw.stop()
        raise HTTPException(500, detail=f"CDP attach failed: {exc}") from exc

    _attached_browsers[session_id] = {
        "pw": pw, "browser": browser, "context": context, "page": page,
        "mode": body.mode,
        # Stream dimensions for coordinate scaling (UC Chrome always runs 1920x1080)
        "stream_width": 1920, "stream_height": 1080,
    }

    # Register in runtime_pool so _get_playwright_page() finds it
    get_runtime_pool()._register(session_id, page, None)

    current_url = page.url
    logger.info("xioview.attached", extra={"session_id": session_id, "url": current_url,
                                            "mode": body.mode})
    return {
        "session_id": session_id,
        "ok": True,
        "current_url": current_url,
        "view_url": f"/api/v1/xioview/sessions/{session_id}/view",
        "mode": body.mode,
    }


@public_router.delete("/attach/{session_id}", summary="Detach and close a manually-attached session")
async def detach_session(session_id: str) -> dict[str, Any]:
    """Close the CDP connection for an attached session."""
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
    old = _attached_browsers.pop(session_id, None)
    # Clean up cached CDP interaction session
    cdp = _interaction_cdp_sessions.pop(session_id, None)
    if cdp:
        try:
            await cdp.detach()
        except Exception:
            pass
    get_runtime_pool()._unregister(session_id)
    if old:
        try:
            await old["browser"].close()
        except Exception:
            pass
        try:
            await old["pw"].stop()
        except Exception:
            pass
    return {"ok": True, "session_id": session_id}


# ── Viewer HTML page ───────────────────────────────────────────────────────────

@public_router.get("/sessions/{session_id}/view", response_class=HTMLResponse,
            summary="Browser viewer page for a session (no auth required)")
async def session_viewer(session_id: str, request: Request) -> HTMLResponse:
    """Serve the self-contained XIOVIEW browser viewer.

    Opens a WebSocket to /observe and renders JPEG frames on a canvas.
    Captures mouse clicks, keyboard, and scroll — relays them as CDP commands.
    """
    # Determine WS base URL from the request host
    host = request.headers.get("host", "localhost:8000")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    ws_url = f"{scheme}://{host}/api/v1/xioview/sessions/{session_id}/observe"

    html = _VIEWER_HTML.replace("__SESSION_ID__", session_id).replace("__WS_URL__", ws_url)
    return HTMLResponse(content=html)


# ── Viewer HTML (self-contained) ───────────────────────────────────────────────

_VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>XIOVIEW — __SESSION_ID__</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0a0a0f; color: #e0e0e0; font-family: 'SF Mono', 'Fira Code', monospace;
       display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

#statusbar {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 12px; background: #111118; border-bottom: 1px solid #222;
  font-size: 11px; flex-shrink: 0; user-select: none;
}
#status-dot { width: 8px; height: 8px; border-radius: 50%; background: #f44; flex-shrink: 0; }
#status-dot.connected { background: #4f4; }
#status-dot.reconnecting { background: #fa0; animation: pulse 0.8s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
#status-label { color: #aaa; }
#session-id { color: #666; font-size: 10px; }
#url-display { flex: 1; color: #7af; font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#fps-display { color: #8f8; }
#mode-select { background: #1a1a2a; color: #ccc; border: 1px solid #333; border-radius: 3px;
               padding: 2px 6px; font-size: 10px; cursor: pointer; }
#focus-badge { font-size: 10px; padding: 1px 7px; border-radius: 3px;
               background: #1a3a1a; color: #4f4; border: 1px solid #2a5a2a; display: none; }
#focus-badge.active { display: inline; }
#ctrl-hint { color: #555; font-size: 10px; }

/* Canvas area — cursor:none so our SVG overlay cursor shows instead */
#canvas-wrap { flex: 1; position: relative; display: flex; align-items: center;
               justify-content: center; overflow: hidden; cursor: none; background: #050508; }
canvas { max-width: 100%; max-height: 100%; image-rendering: auto; display: block; }

/* ── Custom cursor overlay ─────────────────────────────────────────── */
#cursor {
  position: absolute; pointer-events: none; z-index: 999;
  transform: translate(-2px, -2px);   /* hot-spot at top-left tip */
  display: none;
  will-change: left, top;
  filter: drop-shadow(0 0 3px rgba(0,0,0,0.9)) drop-shadow(0 0 1px rgba(0,0,0,1));
}
#cursor.visible { display: block; }

/* Click ripple */
#ripple {
  position: absolute; pointer-events: none; z-index: 998;
  width: 24px; height: 24px; border-radius: 50%;
  border: 2px solid rgba(100,180,255,0.8);
  transform: translate(-50%,-50%) scale(0); opacity: 0;
}
#ripple.pop { animation: ripple-anim 0.35s ease-out forwards; }
@keyframes ripple-anim {
  0%   { transform: translate(-50%,-50%) scale(0.2); opacity: 0.9; }
  100% { transform: translate(-50%,-50%) scale(2.2); opacity: 0; }
}

/* Interaction feedback flash */
#interaction-flash {
  position: absolute; inset: 0; pointer-events: none; z-index: 997;
  background: transparent; transition: background 0.15s;
}
#interaction-flash.success { background: rgba(80,255,80,0.06); }
#interaction-flash.fail    { background: rgba(255,80,80,0.08); }

/* DAG Running badge */
#dag-badge {
  position: absolute; top: 8px; right: 8px; z-index: 100;
  padding: 4px 12px; border-radius: 4px; font-size: 11px;
  background: rgba(200,100,0,0.85); color: #fff; display: none;
  animation: pulse 1.2s infinite;
}
#dag-badge.visible { display: block; }

/* Latency display */
#latency-display { color: #aaa; font-size: 10px; }

/* Connection overlay */
#overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); display: flex;
           align-items: center; justify-content: center; z-index: 100; }
#overlay.hidden { display: none; }
.overlay-box { background: #111; border: 1px solid #333; border-radius: 8px;
               padding: 32px 40px; text-align: center; }
.overlay-box h2 { color: #fa0; margin-bottom: 8px; }
.overlay-box p  { color: #888; font-size: 12px; }
</style>
</head>
<body>

<div id="statusbar">
  <div id="status-dot"></div>
  <span id="status-label">Connecting...</span>
  <span id="session-id">__SESSION_ID__</span>
  <span id="url-display">-</span>
  <span id="fps-display">- fps</span>
  <select id="mode-select">
    <option value="screenshot">Screenshot</option>
    <option value="cdp_screencast" selected>CDP Screencast</option>
    <option value="cdp_dom_snapshot">DOM Snapshot</option>
    <option value="dom_overlay">DOM Overlay</option>
    <option value="dom_stream">DOM Stream (rrweb)</option>
  </select>
  <span id="focus-badge">LIVE CONTROL</span>
  <span id="latency-display"></span>
  <span id="ctrl-hint">Move mouse in to control  Esc to release</span>
</div>

<div id="canvas-wrap">
  <canvas id="screen"></canvas>

  <!-- Custom SVG pointer cursor — larger + dual stroke for visibility on any background -->
  <svg id="cursor" width="28" height="32" viewBox="0 0 28 32" fill="none"
       xmlns="http://www.w3.org/2000/svg">
    <path d="M3 3 L3 27 L9.5 21 L14.5 30 L17.5 28.5 L12.5 19.5 L21 19.5 Z"
          fill="white" stroke="#111" stroke-width="2" stroke-linejoin="round"/>
    <path d="M3 3 L3 27 L9.5 21 L14.5 30 L17.5 28.5 L12.5 19.5 L21 19.5 Z"
          fill="none" stroke="rgba(80,200,255,0.6)" stroke-width="0.8" stroke-linejoin="round"/>
  </svg>

  <!-- Click ripple feedback -->
  <div id="ripple"></div>

  <!-- Interaction feedback flash -->
  <div id="interaction-flash"></div>

  <!-- DAG Running badge -->
  <div id="dag-badge">&#9654; WORKFLOW RUNNING — VIEW ONLY</div>
</div>

<div id="overlay">
  <div class="overlay-box">
    <h2>XIOVIEW</h2>
    <p id="overlay-msg">Connecting to session...</p>
  </div>
</div>

<script>
const SESSION_ID = "__SESSION_ID__";
const WS_BASE    = "__WS_URL__";

const canvas   = document.getElementById("screen");
const ctx      = canvas.getContext("2d");
const wrap     = document.getElementById("canvas-wrap");
const dot      = document.getElementById("status-dot");
const label    = document.getElementById("status-label");
const urlDisp  = document.getElementById("url-display");
const fpsDisp  = document.getElementById("fps-display");
const modesel  = document.getElementById("mode-select");
const overlay  = document.getElementById("overlay");
const ovMsg    = document.getElementById("overlay-msg");
const cursorEl = document.getElementById("cursor");
const ripple   = document.getElementById("ripple");
const badge    = document.getElementById("focus-badge");
const dagBadge = document.getElementById("dag-badge");
const iFlash   = document.getElementById("interaction-flash");
const latDisp  = document.getElementById("latency-display");

let ws = null;
let focused = false;
let frameCount = 0, lastFpsTime = Date.now();
let pageW = 1920, pageH = 1080;
let reconnectDelay = 1000;
let dagRunning = false;
let lastInteractionTs = 0;  // for latency measurement

// FPS counter
setInterval(() => {
  const now = Date.now();
  const elapsed = (now - lastFpsTime) / 1000;
  const fps = elapsed > 0 ? (frameCount / elapsed).toFixed(1) : "0.0";
  fpsDisp.textContent = fps + " fps";
  frameCount = 0; lastFpsTime = now;
}, 2000);

// Focus management — auto-focus on mouseenter, release on mouseleave/Escape
function setFocused(v) {
  focused = v;
  badge.className = v ? "active" : "";
  wrap.style.outline = v ? "2px solid #4af" : "";
  cursorEl.className = v ? "visible" : "";
}
wrap.addEventListener("mouseenter", () => setFocused(true));
wrap.addEventListener("mouseleave", () => { setFocused(false); });

// Interaction feedback flash
function flashInteraction(success) {
  iFlash.className = success ? "success" : "fail";
  setTimeout(() => { iFlash.className = ""; }, 200);
}

// WebSocket
function connect() {
  const mode = modesel.value;
  dot.className = "reconnecting";
  label.textContent = "Connecting...";
  ovMsg.textContent = "Connecting to session...";
  overlay.classList.remove("hidden");

  ws = new WebSocket(WS_BASE + "?mode=" + mode);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    reconnectDelay = 1000;
    dot.className = "connected";
    label.textContent = "Connected";
    overlay.classList.add("hidden");
  };

  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    switch (msg.type) {
      case "frame": {
        if (!msg.jpeg_b64) break;
        const img = new Image();
        img.onload = () => {
          if (canvas.width !== img.width || canvas.height !== img.height) {
            canvas.width = img.width; canvas.height = img.height;
            pageW = img.width; pageH = img.height;
          }
          ctx.drawImage(img, 0, 0);
          frameCount++;
        };
        img.src = "data:image/jpeg;base64," + msg.jpeg_b64;
        break;
      }
      case "connected":   label.textContent = "Live - " + msg.mode; break;
      case "session_info":
        if (msg.url)    urlDisp.textContent = msg.url;
        if (msg.width)  pageW = msg.width;
        if (msg.height) pageH = msg.height;
        break;
      case "interaction_ack": {
        const latMs = Date.now() - lastInteractionTs;
        if (lastInteractionTs > 0) latDisp.textContent = latMs + "ms";
        flashInteraction(msg.success !== false);
        break;
      }
      case "interaction_blocked":
        dagRunning = true;
        dagBadge.className = "visible";
        break;
      case "error":
        ovMsg.textContent = "Error: " + (msg.detail || "unknown");
        overlay.classList.remove("hidden");
        break;
    }
  };

  ws.onclose = () => {
    dot.className = "";
    label.textContent = "Disconnected - reconnecting in " + (reconnectDelay/1000).toFixed(0) + "s";
    overlay.classList.remove("hidden");
    ovMsg.textContent = "Reconnecting...";
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, 15000);
  };
  ws.onerror = () => ws.close();
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    lastInteractionTs = Date.now();
    ws.send(JSON.stringify(msg));
  }
}

// Coordinate mapping: translate viewer pixel coordinates to remote page coordinates.
// Uses the canvas rect (not wrap rect) to avoid letterbox/pillarbox margin errors (RC-5).
function canvasCoords(e) {
  const rect = canvas.getBoundingClientRect();
  const x = (e.clientX - rect.left) * pageW / rect.width;
  const y = (e.clientY - rect.top)  * pageH / rect.height;
  return {
    x: Math.max(0, Math.min(pageW, Math.round(x))),
    y: Math.max(0, Math.min(pageH, Math.round(y))),
  };
}

// Cursor overlay — track position relative to wrap (so it renders over margins too)
wrap.addEventListener("mousemove", (e) => {
  const rect = wrap.getBoundingClientRect();
  cursorEl.style.left = (e.clientX - rect.left) + "px";
  cursorEl.style.top  = (e.clientY - rect.top)  + "px";
  if (!focused) return;
  // Only send mouse_move if pointer is within the canvas bounds
  const cRect = canvas.getBoundingClientRect();
  if (e.clientX >= cRect.left && e.clientX <= cRect.right &&
      e.clientY >= cRect.top  && e.clientY <= cRect.bottom) {
    const {x, y} = canvasCoords(e);
    send({ type: "mouse_move", x, y });
  }
});

// Mouse interaction events — use canvas (not wrap) to avoid margin click issues
canvas.addEventListener("mousedown", (e) => {
  if (!focused) return;
  e.preventDefault();
  // Ripple effect at cursor position within wrap
  const wRect = wrap.getBoundingClientRect();
  ripple.style.left = (e.clientX - wRect.left) + "px";
  ripple.style.top  = (e.clientY - wRect.top)  + "px";
  ripple.className = "";
  void ripple.offsetWidth;   // restart animation
  ripple.className = "pop";
  // Send mousedown
  const {x, y} = canvasCoords(e);
  send({ type: "mousedown", x, y, button: ["left","middle","right"][e.button] || "left" });
});

canvas.addEventListener("mouseup", (e) => {
  if (!focused) return;
  e.preventDefault();
  const {x, y} = canvasCoords(e);
  send({ type: "mouseup", x, y, button: ["left","middle","right"][e.button] || "left" });
});

canvas.addEventListener("click", (e) => {
  if (!focused) return;
  const {x, y} = canvasCoords(e);
  send({ type: "click", x, y, button: ["left","middle","right"][e.button] || "left" });
});

canvas.addEventListener("dblclick", (e) => {
  if (!focused) return;
  e.preventDefault();
  const {x, y} = canvasCoords(e);
  send({ type: "dblclick", x, y });
});

canvas.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  if (!focused) return;
  const {x, y} = canvasCoords(e);
  send({ type: "click", x, y, button: "right" });
});

wrap.addEventListener("wheel", (e) => {
  if (!focused) return;
  e.preventDefault();
  send({ type: "scroll", deltaX: e.deltaX, deltaY: e.deltaY });
}, { passive: false });

document.addEventListener("keydown", (e) => {
  if (!focused) return;
  if (e.key === "Escape") { setFocused(false); return; }
  e.preventDefault();
  const mods = [];
  if (e.ctrlKey)  mods.push("Control");
  if (e.altKey)   mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");
  if (e.metaKey)  mods.push("Meta");
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    send({ type: "type", text: e.key });
  } else {
    send({ type: "key", key: e.key, modifiers: mods });
  }
});

modesel.addEventListener("change", () => { if (ws) ws.close(); });
connect();
</script>
</body>
</html>"""



@public_router.websocket("/sessions/{session_id}/observe")
async def observe_session(
    websocket: WebSocket,
    session_id: str,
    mode: str = Query(default=MODE_SCREENSHOT),
) -> None:
    """Live browser session observation + remote control WebSocket.

    Query params:
      mode    — observation mode: screenshot | cdp_screencast | dom_stream |
                                  cdp_dom_snapshot | dom_overlay
      token   — Bearer token (fallback for WS auth where headers are limited)

    Server → client message types:
      {"type":"connected",           "session_id":"...", "mode":"..."}
      {"type":"frame",               "mode":"screenshot", "jpeg_b64":"..."}
      {"type":"dom_event",           "event":{...rrweb event...}}
      {"type":"dom_snapshot",        "snapshot":{...DOMSnapshot...}}
      {"type":"dom_overlay",         "elements":[...], "count":N}
      {"type":"interaction_ack",     "action":"click", "success":true}
      {"type":"interaction_blocked", "reason":"run_active", "detail":"..."}
      {"type":"action_log",          "action":"click", "node_id":"...", ...}
      {"type":"keepalive"}

    Client → server message types:
      {"type":"mouse_move",       "x":450,"y":230}
      {"type":"mousedown",        "x":450,"y":230,"button":"left"}
      {"type":"mouseup",          "x":450,"y":230,"button":"left"}
      {"type":"click",            "x":450,"y":230,"button":"left"}
      {"type":"dblclick",         "x":450,"y":230}
      {"type":"key",              "key":"Enter","modifiers":[]}
      {"type":"type",             "text":"hello"}
      {"type":"scroll",           "deltaX":0,"deltaY":300}
      {"type":"pause_workflow",   "run_id":"..."}
      {"type":"resume_workflow",  "run_id":"..."}
      {"type":"set_fps",          "fps":2.0}
    """
    if mode not in VALID_MODES:
        await websocket.close(code=4400, reason=f"invalid_mode: {mode}")
        return

    await websocket.accept()
    org_id = _get_org_id(websocket)
    reg = get_registry()
    queue: asyncio.Queue = asyncio.Queue(maxsize=30)

    logger.info("xioview.client_connected", extra={
        "session_id": session_id, "org_id": org_id, "mode": mode,
    })

    try:
        # Validate session (best-effort — WS has no HTTP session middleware)
        # Skip DB check for manually-attached sessions: they are registered in
        # _attached_browsers by the POST /xioview/attach endpoint (which does its own
        # auth check), and have no row in browser_sessions. Querying the DB for them
        # always returns 404 and immediately closes the WebSocket — Bug 1 fix.
        _is_attached_session = session_id in _attached_browsers
        db: OrmSession | None = getattr(websocket.state, "org_session", None)
        if db and not _is_attached_session:
            try:
                _assert_session_visible(session_id, org_id, db)
            except HTTPException as e:
                await websocket.send_json({"type": "error", "detail": e.detail})
                await websocket.close(code=4403)
                return

        # Register client in registry — starts capture task if first client.
        # Only pass page_getter for screenshot mode to prevent _capture_loop
        # from running concurrently with CDP screencast or DOM stream (RC-8).
        _needs_capture = mode == MODE_SCREENSHOT
        entry = reg.add_client(
            session_id=session_id,
            org_id=org_id,
            mode=mode,
            queue=queue,
            page_getter=lambda: _get_playwright_page(session_id) if _needs_capture else None,
        )

        # Send connected confirmation + last good frame immediately
        await websocket.send_json({
            "type": "connected",
            "session_id": session_id,
            "mode": mode,
            "fps": entry.fps,
        })
        if entry.last_frame and mode == MODE_SCREENSHOT:
            import base64
            await websocket.send_json({
                "type": "frame",
                "mode": "screenshot",
                "jpeg_b64": base64.b64encode(entry.last_frame).decode(),
            })

        # Push session_info (current page URL + viewport dims) so viewer updates immediately
        try:
            _page = await _get_playwright_page(session_id)
            if _page:
                _vp = await _page.evaluate("() => ({ w: window.innerWidth, h: window.innerHeight })")
                _sw = entry.stream_width
                _sh = entry.stream_height
                entry.viewport_width = _vp.get("w", _sw)
                entry.viewport_height = _vp.get("h", _sh)
                await websocket.send_json({
                    "type": "session_info",
                    "url": _page.url,
                    "width": _sw,
                    "height": _sh,
                })
        except Exception:
            pass

        # If CDP screencast or DOM stream mode, start those modes separately
        cdp_task: asyncio.Task | None = None
        if mode == MODE_CDP_SCREENCAST:
            cdp_task = asyncio.create_task(
                _cdp_screencast_loop(session_id, queue),
                name=f"xioview-cdp-{session_id[:8]}",
            )
        elif mode == MODE_DOM_STREAM:
            asyncio.create_task(
                _inject_rrweb(session_id, queue),
                name=f"xioview-rrweb-{session_id[:8]}",
            )
        elif mode == MODE_CDP_DOM_SNAPSHOT:
            from xiosync.subsystems.xioview.modes.cdp_dom_snapshot import (  # noqa: PLC0415
                cdp_dom_snapshot_loop,
            )
            asyncio.create_task(
                cdp_dom_snapshot_loop(
                    session_id, queue,
                    get_page=lambda: _get_playwright_page(session_id),
                ),
                name=f"xioview-domsnapshot-{session_id[:8]}",
            )
        elif mode == MODE_DOM_OVERLAY:
            # DOM Overlay = CDP screencast (visual) + DOM element hitboxes (semantic)
            cdp_task = asyncio.create_task(
                _cdp_screencast_loop(session_id, queue),
                name=f"xioview-cdp-{session_id[:8]}",
            )
            from xiosync.subsystems.xioview.modes.dom_overlay import (  # noqa: PLC0415
                dom_overlay_loop,
            )
            asyncio.create_task(
                dom_overlay_loop(
                    session_id, queue,
                    get_page=lambda: _get_playwright_page(session_id),
                ),
                name=f"xioview-domoverlay-{session_id[:8]}",
            )

        # Concurrent: send queued frames to client + receive control commands
        send_task = asyncio.create_task(_sender(websocket, queue))
        recv_task = asyncio.create_task(_receiver(websocket, session_id, org_id))

        done, pending = await asyncio.wait(
            [send_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if cdp_task:
            cdp_task.cancel()

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("xioview.session_error", extra={
            "session_id": session_id, "error": str(exc)
        })
    finally:
        reg.remove_client(session_id, queue)
        logger.info("xioview.client_disconnected", extra={"session_id": session_id})


# ── Internal coroutines ────────────────────────────────────────────────────────

async def _sender(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Pump queued messages to the WebSocket client. Sends keepalives every 30s."""
    _keepalive = json.dumps({"type": "keepalive"})
    while True:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=30.0)
            await websocket.send_text(msg)
        except TimeoutError:
            # send_text (not send_json) for consistency — client decodes all messages uniformly
            await websocket.send_text(_keepalive)
        except (WebSocketDisconnect, RuntimeError):
            break


async def _receiver(websocket: WebSocket, session_id: str, org_id: str) -> None:
    """Receive and dispatch remote-control commands from the operator."""
    while True:
        try:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
        except (WebSocketDisconnect, RuntimeError):
            break
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type", "")
        try:
            await _dispatch_control(msg_type, msg, session_id, org_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("xioview.control_error", extra={
                "type": msg_type, "error": str(exc), "session_id": session_id
            })


async def _set_run_state(
    run_id: str, org_id: str, new_state: str, valid_from: tuple[str, ...]
) -> None:
    """Directly transition a workflow run state — used by HITL pause/resume from XIOVIEW.

    Bypasses the HTTP self-call pattern; writes directly to xioflow_runs using the
    application engine under the correct org RLS context. Best-effort: any error
    is swallowed so the WebSocket session is never torn down by a pause/resume failure.
    """
    try:
        from sqlalchemy import text as _text
        from xiosync.platform.engine_ref import get_engine

        engine = get_engine()
        if engine is None:
            logger.warning("xioview.hitl_no_engine — engine_ref not yet initialized")
            return

        import asyncio
        loop = asyncio.get_running_loop()

        def _write() -> None:
            from sqlalchemy.orm import Session as _Session
            with _Session(engine) as s, s.begin():
                s.execute(_text(
                    "SELECT set_config('app.current_org', :org, true)"
                ), {"org": org_id})
                result = s.execute(_text("""
                    UPDATE xioflow_runs
                    SET    state = :new_state
                    WHERE  id    = :id
                      AND  organization_id = :org
                      AND  state = ANY(:valid_from)
                """), {
                    "new_state": new_state,
                    "id": run_id,
                    "org": org_id,
                    "valid_from": list(valid_from),
                })
                if result.rowcount == 0:
                    logger.warning("xioview.hitl_run_not_found_or_wrong_state", extra={
                        "run_id": run_id, "new_state": new_state,
                    })

        await loop.run_in_executor(None, _write)
        logger.info("xioview.hitl_state_set", extra={
            "run_id": run_id, "new_state": new_state, "org_id": org_id,
        })

    except Exception as exc:  # noqa: BLE001
        logger.warning("xioview.hitl_set_run_state_failed", extra={
            "run_id": run_id, "error": str(exc),
        })


async def _dispatch_control(
    msg_type: str, msg: dict[str, Any], session_id: str, org_id: str
) -> None:
    """Route incoming operator commands to remote Chrome via raw CDP.

    Uses CDP Input.dispatchMouseEvent / Input.dispatchKeyEvent for isTrusted=true
    events that pass bot detection, matching the pattern used by xiorun_agent.py.
    Replaces the previous Playwright page.mouse/keyboard API calls which caused
    RC-1 (coordinate mismatch) and RC-3 (no isTrusted events).
    """
    # Non-page commands (workflow control, FPS) are always allowed
    if msg_type == "pause_workflow":
        run_id = msg.get("run_id")
        if run_id:
            await _set_run_state(run_id, org_id, "PAUSED", ("RUNNING", "PENDING"))
        return
    elif msg_type == "resume_workflow":
        run_id = msg.get("run_id")
        if run_id:
            await _set_run_state(run_id, org_id, "PENDING", ("PAUSED",))
        return
    elif msg_type == "set_fps":
        fps = float(msg.get("fps", 5.0))
        reg = get_registry()
        entry = reg._sessions.get(session_id)
        if entry:
            entry.fps = max(0.1, min(15.0, fps))
        return

    # Get page — required for all interaction commands
    page = await _get_playwright_page(session_id)
    if page is None:
        logger.warning("xioview.no_page_for_control", extra={"session_id": session_id})
        return

    # Block manual interactions while any run is executing (script or DAG — Q2)
    if msg_type in ("click", "mousedown", "mouseup", "dblclick", "type", "key",
                     "scroll", "mouse_move") and _is_run_active(session_id):
        get_registry().push_event(session_id, {
            "type": "interaction_blocked",
            "reason": "run_active",
            "detail": "Interactions are blocked while a workflow is executing. "
                      "Pause or wait for the run to complete to interact manually.",
        })
        return

    reg = get_registry()
    ack_event: dict[str, Any] | None = None

    try:
        if msg_type == "mouse_move":
            x, y = await _scale_coords(session_id, page, msg["x"], msg["y"])
            cdp = await _get_or_create_cdp_session(session_id, page)
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": x, "y": y,
            })

        elif msg_type == "mousedown":
            x, y = await _scale_coords(session_id, page, msg["x"], msg["y"])
            cdp = await _get_or_create_cdp_session(session_id, page)
            button = msg.get("button", "left")
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "button": button,
                "clickCount": 1, "x": x, "y": y,
            })
            ack_event = {"type": "interaction_ack", "action": "mousedown",
                         "x": msg["x"], "y": msg["y"], "success": True}

        elif msg_type == "mouseup":
            x, y = await _scale_coords(session_id, page, msg["x"], msg["y"])
            cdp = await _get_or_create_cdp_session(session_id, page)
            button = msg.get("button", "left")
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "button": button,
                "clickCount": 1, "x": x, "y": y,
            })
            ack_event = {"type": "interaction_ack", "action": "mouseup",
                         "x": msg["x"], "y": msg["y"], "success": True}

        elif msg_type == "click":
            # Full click lifecycle: mousePressed → short delay → mouseReleased
            x, y = await _scale_coords(session_id, page, msg["x"], msg["y"])
            cdp = await _get_or_create_cdp_session(session_id, page)
            button = msg.get("button", "left")
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "button": button,
                "clickCount": 1, "x": x, "y": y,
            })
            await asyncio.sleep(0.05)  # realistic press-release gap
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "button": button,
                "clickCount": 1, "x": x, "y": y,
            })
            ack_event = {"type": "interaction_ack", "action": "click",
                         "x": msg["x"], "y": msg["y"], "success": True}

        elif msg_type == "dblclick":
            x, y = await _scale_coords(session_id, page, msg["x"], msg["y"])
            cdp = await _get_or_create_cdp_session(session_id, page)
            for click_count in (1, 2):
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "button": "left",
                    "clickCount": click_count, "x": x, "y": y,
                })
                await asyncio.sleep(0.04)
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "button": "left",
                    "clickCount": click_count, "x": x, "y": y,
                })
                if click_count == 1:
                    await asyncio.sleep(0.08)  # inter-click gap
            ack_event = {"type": "interaction_ack", "action": "dblclick",
                         "x": msg["x"], "y": msg["y"], "success": True}

        elif msg_type == "key":
            key = msg.get("key", "")
            mods = msg.get("modifiers", [])
            cdp = await _get_or_create_cdp_session(session_id, page)
            # Use Playwright for key combos — it handles modifier mapping correctly
            for mod in mods:
                await page.keyboard.down(mod)
            await page.keyboard.press(key)
            for mod in reversed(mods):
                await page.keyboard.up(mod)
            ack_event = {"type": "interaction_ack", "action": "key",
                         "key": key, "success": True}

        elif msg_type == "type":
            text = msg.get("text", "")
            # Type char-by-char via CDP for isTrusted=true key events
            cdp = await _get_or_create_cdp_session(session_id, page)
            for char in text:
                await cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyDown", "text": char, "key": char,
                    "code": f"Key{char.upper()}" if char.isalpha() else "",
                })
                await cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": char,
                    "code": f"Key{char.upper()}" if char.isalpha() else "",
                })
                await asyncio.sleep(0.02)  # realistic typing cadence
            ack_event = {"type": "interaction_ack", "action": "type",
                         "text_len": len(text), "success": True}

        elif msg_type == "scroll":
            cdp = await _get_or_create_cdp_session(session_id, page)
            # CDP mouseWheel needs an x,y origin — use center of viewport
            x, y = 960, 540  # sensible default
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": x, "y": y,
                "deltaX": msg.get("deltaX", 0),
                "deltaY": msg.get("deltaY", 0),
            })

    except Exception as exc:
        logger.warning("xioview.control_dispatch_failed", extra={
            "session_id": session_id, "msg_type": msg_type, "error": str(exc),
        })
        ack_event = {"type": "interaction_ack", "action": msg_type,
                     "success": False, "error": str(exc)}

    # Send interaction acknowledgement to viewer
    if ack_event is not None:
        reg.push_event(session_id, ack_event)


# ── CDP Screencast mode ────────────────────────────────────────────────────────

async def _cdp_screencast_loop(session_id: str, queue: asyncio.Queue) -> None:
    """Capture and stream Chrome frames via CDP.

    Two-phase auto-detection:

    Phase 1 — CDP Page.startScreencast (event-driven, preferred):
      Chrome pushes JPEG frames. Works when GPU compositor is active.
      Falls back after 3s if no frames arrive — happens on UC Chrome launched
      via undetected-chromedriver on Xvfb where the compositor does not push.

    Phase 2 — Polled Page.captureScreenshot (~3 fps):
      Raw CDP call that always works on any Chrome regardless of compositor.
      Proven on UC Chrome 131 + Xvfb + SwiftShader.
    """
    import json as _json

    page = await _get_playwright_page(session_id)
    if page is None:
        logger.warning("xioview.cdp_no_page", extra={"session_id": session_id})
        return

    STREAM_W, STREAM_H = 1920, 1080
    try:
        queue.put_nowait(_json.dumps({
            "type": "session_info",
            "url": page.url,
            "width": STREAM_W,
            "height": STREAM_H,
        }))
    except asyncio.QueueFull:
        pass

    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception as exc:
        logger.warning("xioview.cdp_session_failed", extra={
            "session_id": session_id, "error": str(exc)
        })
        return

    # Phase 1: event-driven screencast
    _frames: list[int] = [0]

    def on_frame(event: dict[str, Any]) -> None:
        data = event.get("data", "")
        frame_no = event.get("sessionId", 0)
        _frames[0] += 1
        try:
            queue.put_nowait(_json.dumps({
                "type": "frame", "mode": "cdp_screencast", "jpeg_b64": data,
            }))
        except asyncio.QueueFull:
            pass
        try:
            asyncio.get_running_loop().create_task(
                cdp.send("Page.screencastFrameAck", {"sessionId": frame_no})
            )
        except RuntimeError:
            pass

    cdp.on("Page.screencastFrame", on_frame)
    try:
        await cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 70,
            "maxWidth": STREAM_W, "maxHeight": STREAM_H,
            "everyNthFrame": 1,
        })
        logger.info("xioview.cdp_screencast_started", extra={
            "session_id": session_id, "res": f"{STREAM_W}x{STREAM_H}"
        })
    except Exception as exc:
        logger.warning("xioview.cdp_screencast_start_failed", extra={
            "session_id": session_id, "error": str(exc)
        })

    # Wait 3s for frames
    try:
        await asyncio.sleep(3.0)
    except asyncio.CancelledError:
        try:
            await cdp.send("Page.stopScreencast")
            await cdp.detach()
        except Exception:
            pass
        return

    if _frames[0] > 0:
        # Screencast working — keep alive
        logger.info("xioview.cdp_screencast_live", extra={
            "session_id": session_id, "frames_3s": _frames[0]
        })
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
            except Exception:
                pass
        return

    # Phase 2: no frames — fall back to polling Page.captureScreenshot
    logger.info("xioview.cdp_screencast_poll_fallback", extra={
        "session_id": session_id,
        "reason": "0 frames in 3s — UC Chrome/Xvfb, falling back to poll",
    })
    try:
        await cdp.send("Page.stopScreencast")
    except Exception:
        pass

    POLL_INTERVAL = 0.35  # ~3 fps
    try:
        while True:
            try:
                result = await cdp.send("Page.captureScreenshot", {
                    "format": "jpeg", "quality": 70,
                    "fromSurface": True, "captureBeyondViewport": False,
                })
                jpeg_b64 = result.get("data", "")
                if jpeg_b64:
                    try:
                        queue.put_nowait(_json.dumps({
                            "type": "frame", "mode": "cdp_screencast",
                            "jpeg_b64": jpeg_b64,
                        }))
                    except asyncio.QueueFull:
                        pass
                    _frames[0] += 1
                    # Refresh URL every ~10s
                    if _frames[0] % 30 == 0:
                        try:
                            queue.put_nowait(_json.dumps({
                                "type": "session_info",
                                "url": page.url,
                                "width": STREAM_W, "height": STREAM_H,
                            }))
                        except asyncio.QueueFull:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("xioview.poll_screenshot_error", extra={
                    "session_id": session_id, "error": str(exc)
                })
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        try:
            await cdp.detach()
        except Exception:
            pass


# ── rrweb DOM stream mode ──────────────────────────────────────────────────────

_RRWEB_JS_PATH = None  # resolved lazily


def _rrweb_path() -> str | None:
    """Return path to vendored rrweb.min.js, or None if not present."""
    global _RRWEB_JS_PATH
    if _RRWEB_JS_PATH is not None:
        return _RRWEB_JS_PATH
    import pathlib
    candidates = [
        pathlib.Path(__file__).parent.parent.parent.parent.parent / "tools" / "static" / "rrweb.min.js",
        pathlib.Path(__file__).parent / "static" / "rrweb.min.js",
    ]
    for p in candidates:
        if p.exists():
            _RRWEB_JS_PATH = str(p)
            return _RRWEB_JS_PATH
    return None


async def _inject_rrweb(session_id: str, queue: asyncio.Queue) -> None:
    """Inject rrweb recorder into the page and bridge events to the WS queue.

    Falls back gracefully if rrweb.min.js is not yet vendored. The caller
    should download rrweb.min.js from https://github.com/rrweb-io/rrweb and
    place it at tools/static/rrweb.min.js.
    """
    import json as _json

    path = _rrweb_path()
    if path is None:
        msg = _json.dumps({
            "type": "error",
            "detail": "rrweb not vendored — place rrweb.min.js at tools/static/rrweb.min.js",
        })
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass
        return

    page = await _get_playwright_page(session_id)
    if page is None:
        return

    try:
        # expose_function bridges JS → Python synchronously (Playwright internals)
        async def _emit(event: Any) -> None:
            msg = _json.dumps({"type": "dom_event", "event": event})
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

        await page.expose_function("__xioview_emit", _emit)
        await page.add_init_script(path=path)
        await page.evaluate("""() => {
            if (typeof rrweb !== 'undefined') {
                rrweb.record({ emit: window.__xioview_emit });
            }
        }""")
        logger.info("xioview.rrweb_injected", extra={"session_id": session_id})

    except Exception as exc:  # noqa: BLE001
        logger.warning("xioview.rrweb_inject_failed", extra={
            "session_id": session_id, "error": str(exc)
        })
