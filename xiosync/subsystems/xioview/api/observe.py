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
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioview", tags=["XIOVIEW"])

# Public router — endpoints that DON'T need a JWT (attach uses internal-secret,
# viewer HTML is fully public). Mounted in app.py WITHOUT require_capability.
public_router = APIRouter(prefix="/xioview", tags=["XIOVIEW-public"])

# ── Manually-attached CDP sessions (outside normal BrowserLauncher flow) ───────
# session_id → {pw, browser, context, page}
_attached_browsers: dict[str, dict[str, Any]] = {}



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


@public_router.post("/attach", summary="Attach XIOSYNC to an existing Colab Chrome CDP session")
async def attach_session(body: AttachRequest) -> dict[str, Any]:
    """Connect XIOSYNC's patchright to a running Chrome via CDP URL.

    Use this for manually-launched or test sessions before going through the
    full BrowserLauncher flow. After attaching, open:
      GET /api/v1/xioview/sessions/{session_id}/view
    to observe and control the browser.

    Auth: pass XIOSYNC_INTERNAL_SECRET as internal_secret in the request body,
    OR call from inside the Tailscale network (no external exposure).
    """
    # Lightweight auth — internal secret check (JWT not required for this endpoint)
    expected = os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    if expected and body.internal_secret != expected:
        raise HTTPException(403, detail="invalid_internal_secret")

    from patchright.async_api import async_playwright  # noqa: PLC0415
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

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
    logger.info("xioview.attach_start", extra={"session_id": session_id, "url": cdp_http_url})

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
    }

    # Register in runtime_pool so _get_playwright_page() finds it
    get_runtime_pool()._register(session_id, page, None)

    current_url = page.url
    logger.info("xioview.attached", extra={"session_id": session_id, "url": current_url})
    return {
        "session_id": session_id,
        "ok": True,
        "current_url": current_url,
        "view_url": f"/api/v1/xioview/sessions/{session_id}/view",
    }


@public_router.delete("/attach/{session_id}", summary="Detach and close a manually-attached session")
async def detach_session(session_id: str) -> dict[str, Any]:
    """Close the CDP connection for an attached session."""
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
    old = _attached_browsers.pop(session_id, None)
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
#ctrl-hint { color: #555; font-size: 10px; }

#canvas-wrap { flex: 1; display: flex; align-items: center; justify-content: center;
               overflow: hidden; cursor: crosshair; background: #050508; }
canvas { max-width: 100%; max-height: 100%; image-rendering: auto; }

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
  <span id="status-label">Connecting…</span>
  <span id="session-id">__SESSION_ID__</span>
  <span id="url-display">—</span>
  <span id="fps-display">— fps</span>
  <select id="mode-select">
    <option value="screenshot" selected>Screenshot</option>
    <option value="cdp_screencast">CDP Screencast</option>
  </select>
  <span id="ctrl-hint">Click to focus · Esc to release</span>
</div>

<div id="canvas-wrap" id="wrap">
  <canvas id="screen"></canvas>
</div>

<div id="overlay">
  <div class="overlay-box">
    <h2>XIOVIEW</h2>
    <p id="overlay-msg">Connecting to session…</p>
  </div>
</div>

<script>
const SESSION_ID = "__SESSION_ID__";
const WS_BASE    = "__WS_URL__";

const canvas  = document.getElementById("screen");
const ctx     = canvas.getContext("2d");
const wrap    = document.getElementById("canvas-wrap");
const dot     = document.getElementById("status-dot");
const label   = document.getElementById("status-label");
const urlDisp = document.getElementById("url-display");
const fpsDisp = document.getElementById("fps-display");
const modesel = document.getElementById("mode-select");
const overlay = document.getElementById("overlay");
const ovMsg   = document.getElementById("overlay-msg");

let ws = null;
let focused = false;
let frameCount = 0, lastFpsTime = Date.now();
let pageW = 1920, pageH = 1080;  // remote page dimensions
let reconnectDelay = 1000;

// FPS counter
setInterval(() => {
  const now = Date.now();
  const fps = (frameCount / ((now - lastFpsTime) / 1000)).toFixed(1);
  fpsDisp.textContent = fps + " fps";
  frameCount = 0; lastFpsTime = now;
}, 2000);

function connect() {
  const mode = modesel.value;
  const url  = WS_BASE + "?mode=" + mode;
  dot.className = "reconnecting";
  label.textContent = "Connecting…";
  ovMsg.textContent = "Connecting to session…";
  overlay.classList.remove("hidden");

  ws = new WebSocket(url);
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
        const jpeg = msg.jpeg_b64;
        if (!jpeg) break;
        const img = new Image();
        img.onload = () => {
          // Resize canvas to match image if changed
          if (canvas.width !== img.width || canvas.height !== img.height) {
            canvas.width  = img.width;
            canvas.height = img.height;
            pageW = img.width; pageH = img.height;
          }
          ctx.drawImage(img, 0, 0);
          frameCount++;
        };
        img.src = "data:image/jpeg;base64," + jpeg;
        break;
      }
      case "connected":
        label.textContent = "Live — " + msg.mode;
        break;
      case "session_info":
        if (msg.url) urlDisp.textContent = msg.url;
        if (msg.width)  pageW = msg.width;
        if (msg.height) pageH = msg.height;
        break;
      case "keepalive": break;
      case "error":
        ovMsg.textContent = "Error: " + (msg.detail || "unknown");
        overlay.classList.remove("hidden");
        break;
    }
  };

  ws.onclose = () => {
    dot.className = "";
    label.textContent = "Disconnected — reconnecting in " + (reconnectDelay/1000).toFixed(0) + "s";
    overlay.classList.remove("hidden");
    ovMsg.textContent = "Reconnecting…";
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, 15000);
  };

  ws.onerror = () => ws.close();
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

// ── Coordinate mapping ────────────────────────────────────────────────────────
function canvasCoords(e) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = pageW / rect.width;
  const scaleY = pageH / rect.height;
  return {
    x: Math.round((e.clientX - rect.left) * scaleX),
    y: Math.round((e.clientY - rect.top)  * scaleY),
  };
}

// ── Mouse events ─────────────────────────────────────────────────────────────
wrap.addEventListener("click", (e) => {
  if (!focused) { focused = true; wrap.style.outline = "2px solid #4af"; return; }
  const {x, y} = canvasCoords(e);
  const btn = ["left","middle","right"][e.button] || "left";
  send({ type: "click", x, y, button: btn });
});

wrap.addEventListener("mousemove", (e) => {
  if (!focused) return;
  const {x, y} = canvasCoords(e);
  send({ type: "mouse_move", x, y });
});

wrap.addEventListener("contextmenu", (e) => {
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

// ── Keyboard events ───────────────────────────────────────────────────────────
document.addEventListener("keydown", (e) => {
  if (!focused) return;
  if (e.key === "Escape") {
    focused = false;
    wrap.style.outline = "";
    return;
  }
  e.preventDefault();
  const mods = [];
  if (e.ctrlKey)  mods.push("Control");
  if (e.altKey)   mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");
  if (e.metaKey)  mods.push("Meta");

  // For printable characters, use type; for special keys, use key press
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    send({ type: "type", text: e.key });
  } else {
    send({ type: "key", key: e.key, modifiers: mods });
  }
});

// ── Mode change ───────────────────────────────────────────────────────────────
modesel.addEventListener("change", () => {
  if (ws) ws.close();  // reconnect with new mode
});

// ── Start ─────────────────────────────────────────────────────────────────────
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
      mode    — observation mode: screenshot | cdp_screencast | dom_stream
      token   — Bearer token (fallback for WS auth where headers are limited)

    Server → client message types:
      {"type":"connected",   "session_id":"...", "mode":"..."}
      {"type":"frame",       "mode":"screenshot", "jpeg_b64":"..."}
      {"type":"dom_event",   "event":{...rrweb event...}}
      {"type":"action_log",  "action":"click", "node_id":"...", ...}
      {"type":"keepalive"}

    Client → server message types:
      {"type":"mouse_move",       "x":450,"y":230}
      {"type":"click",            "x":450,"y":230,"button":"left"}
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
        # We do a quick direct DB check using the pool from app state
        db: OrmSession | None = getattr(websocket.state, "org_session", None)
        if db:
            try:
                _assert_session_visible(session_id, org_id, db)
            except HTTPException as e:
                await websocket.send_json({"type": "error", "detail": e.detail})
                await websocket.close(code=4403)
                return

        # Register client in registry — starts capture task if first client
        entry = reg.add_client(
            session_id=session_id,
            org_id=org_id,
            mode=mode,
            queue=queue,
            page_getter=lambda: _get_playwright_page(session_id),
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

        # Push session_info (current page URL) so viewer URL bar updates immediately
        try:
            _page = await _get_playwright_page(session_id)
            if _page:
                await websocket.send_json({
                    "type": "session_info",
                    "url": _page.url,
                    "width": 1920,
                    "height": 1080,
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
        except asyncio.TimeoutError:
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
    """Route incoming operator commands to Playwright page actions."""
    page = await _get_playwright_page(session_id)
    if page is None and msg_type not in ("pause_workflow", "resume_workflow", "set_fps"):
        logger.warning("xioview.no_page_for_control", extra={"session_id": session_id})
        return

    if msg_type == "mouse_move":
        await page.mouse.move(msg["x"], msg["y"])

    elif msg_type == "click":
        button = msg.get("button", "left")
        await page.mouse.click(msg["x"], msg["y"], button=button)

    elif msg_type == "key":
        key = msg.get("key", "")
        mods = msg.get("modifiers", [])
        for mod in mods:
            await page.keyboard.down(mod)
        await page.keyboard.press(key)
        for mod in reversed(mods):
            await page.keyboard.up(mod)

    elif msg_type == "type":
        await page.keyboard.type(msg.get("text", ""))

    elif msg_type == "scroll":
        await page.mouse.wheel(msg.get("deltaX", 0), msg.get("deltaY", 0))

    elif msg_type == "pause_workflow":
        run_id = msg.get("run_id")
        if run_id:
            await _set_run_state(run_id, org_id, "PAUSED", ("RUNNING", "PENDING"))

    elif msg_type == "resume_workflow":
        run_id = msg.get("run_id")
        if run_id:
            await _set_run_state(run_id, org_id, "PENDING", ("PAUSED",))

    elif msg_type == "set_fps":
        fps = float(msg.get("fps", 5.0))
        reg = get_registry()
        entry = reg._sessions.get(session_id)
        if entry:
            entry.fps = max(0.1, min(15.0, fps))


# ── CDP Screencast mode ────────────────────────────────────────────────────────

async def _cdp_screencast_loop(session_id: str, queue: asyncio.Queue) -> None:
    """Activate Chrome DevTools Protocol screencast for GPU-accelerated streaming.

    Delivers JPEG frames via CDP Page.screencastFrame events — more efficient
    than polling page.screenshot() at equivalent FPS. Chromium-only.
    """
    import base64
    import json as _json

    page = await _get_playwright_page(session_id)
    if page is None:
        logger.warning("xioview.cdp_no_page", extra={"session_id": session_id})
        return

    try:
        cdp = await page.context.new_cdp_session(page)

        def on_frame(event: dict[str, Any]) -> None:
            data = event.get("data", "")
            msg = _json.dumps({
                "type": "frame",
                "mode": "cdp_screencast",
                "jpeg_b64": data,
            })
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 65,
            "maxWidth": 1280,
            "maxHeight": 800,
            "everyNthFrame": 1,
        })

        # Keep alive until cancelled
        while True:
            await asyncio.sleep(60)

    except asyncio.CancelledError:
        try:
            await cdp.send("Page.stopScreencast")
            await cdp.detach()
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("xioview.cdp_screencast_error", extra={
            "session_id": session_id, "error": str(exc)
        })


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
