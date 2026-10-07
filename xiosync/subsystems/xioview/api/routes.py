"""XIOVIEW API Routes — FastAPI WebSocket and REST endpoints.

Thin routing layer that delegates to:
  - session_manager.py  — browser attach/detach and page resolution
  - control.py          — input dispatch, coordinate scaling, run guards
  - screencast.py       — CDP screencast and rrweb streaming
  - viewer.py           — HTML viewer rendering
  - registry.py         — session tracking and frame queuing
  - modes/              — cdp_dom_snapshot, dom_overlay

Endpoints:
  WS  /xioview/sessions/{session_id}/observe   — live observation + control
  GET /xioview/sessions                        — list observed sessions
  GET /xioview/observable                      — list all observable sessions
  POST /xioview/sessions/{id}/fps              — set adaptive FPS
  POST /xioview/attach                         — attach to manual CDP session
  DELETE /xioview/attach/{session_id}           — detach session
  GET /xioview/sessions/{id}/view              — viewer HTML page
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from xiosync.subsystems.xioview.protocol import (
    MODE_CDP_DOM_SNAPSHOT,
    MODE_CDP_SCREENCAST,
    MODE_DOM_OVERLAY,
    MODE_DOM_STREAM,
    MODE_SCREENSHOT,
    MSG_CONNECTED,
    MSG_ERROR,
    MSG_FRAME,
    MSG_KEEPALIVE,
    MSG_SESSION_INFO,
)
from xiosync.subsystems.xioview.registry import (
    VALID_MODES,
    get_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioview", tags=["XIOVIEW"])

# Public router — endpoints that DON'T need a JWT.
# Mounted in app.py WITHOUT require_capability.
public_router = APIRouter(prefix="/xioview", tags=["XIOVIEW-public"])


# ── Helpers ────────────────────────────────────────────────────────────────────


def _get_org_id(websocket: WebSocket) -> str:
    """Extract org_id from the WebSocket's auth state."""
    ctx = getattr(websocket.state, "org_context", None)
    if ctx:
        return str(ctx.organization_id)
    return websocket.query_params.get("org_id", "")


def _assert_session_visible(session_id: str, org_id: str, db: Any) -> dict[str, Any]:
    """Verify the session exists, belongs to this org, and is in a live state."""
    from sqlalchemy import text  # noqa: PLC0415

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
    if row.state not in ("active", "initializing"):
        raise HTTPException(
            status_code=409,
            detail=f"session_not_live (state={row.state})",
        )
    return {"engine_type": row.engine_type, "state": row.state}


# ── REST Endpoints ─────────────────────────────────────────────────────────────


@router.get("/sessions", summary="List all actively observed browser sessions")
def list_observed_sessions() -> dict[str, Any]:
    return {"sessions": get_registry().list_sessions()}


@router.get(
    "/observable",
    summary="List ALL observable browser sessions (live CDP + actively streamed)",
)
def list_observable_sessions() -> dict[str, Any]:
    """Full multi-session grid data for XIOVIEW UI."""
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

    observed: dict[str, dict] = {
        s["session_id"]: {**s, "streaming": True} for s in get_registry().list_sessions()
    }

    for entry in get_runtime_pool().list_sessions():
        sid = entry["session_id"]
        if sid not in observed:
            observed[sid] = {
                "session_id": sid,
                "page_closed": entry["page_closed"],
                "streaming": False,
                "mode": None,
                "fps": None,
            }

    sessions = list(observed.values())
    return {
        "sessions": sessions,
        "total": len(sessions),
        "streaming_count": sum(1 for s in sessions if s.get("streaming")),
        "observable_count": sum(1 for s in sessions if not s.get("page_closed")),
    }


@router.post("/sessions/{session_id}/fps", summary="Set adaptive FPS for a session")
def set_session_fps(session_id: str, fps: float = Query(ge=0.1, le=15)) -> dict[str, Any]:
    reg = get_registry()
    entry = reg._sessions.get(session_id)
    if not entry:
        raise HTTPException(404, detail="session_not_observed")
    entry.fps = fps
    return {"session_id": session_id, "fps": fps}


@router.get(
    "/sessions/{session_id}/tabs",
    summary="List all tabs (pages) in a browser session (Q-8)",
)
async def list_session_tabs(session_id: str) -> dict[str, Any]:
    """Return all open tabs for a session so the operator can switch views."""
    from xiosync.subsystems.xioview.session_manager import get_attached_info  # noqa: PLC0415

    info = get_attached_info(session_id)
    if info is None:
        raise HTTPException(404, detail="session_not_attached")

    context = info.get("context")
    if context is None:
        return {"session_id": session_id, "tabs": [], "active_index": 0}

    tabs = []
    active_page = info.get("page")
    active_index = 0
    for i, page in enumerate(context.pages):
        if page is active_page:
            active_index = i
        tabs.append(
            {
                "index": i,
                "url": page.url,
                "title": await page.title() if not page.is_closed() else "(closed)",
                "closed": page.is_closed(),
                "active": page is active_page,
            }
        )

    return {
        "session_id": session_id,
        "tabs": tabs,
        "active_index": active_index,
        "count": len(tabs),
    }


@router.post(
    "/sessions/{session_id}/tabs/{tab_index}",
    summary="Switch the observed tab to a different page index (Q-8)",
)
async def switch_session_tab(session_id: str, tab_index: int) -> dict[str, Any]:
    """Switch XIOVIEW observation to a different tab within the session."""
    from xiosync.subsystems.xioview.session_manager import (  # noqa: PLC0415
        get_attached_info,
        invalidate_cdp_session,
        invalidate_viewport_cache,
    )

    info = get_attached_info(session_id)
    if info is None:
        raise HTTPException(404, detail="session_not_attached")

    context = info.get("context")
    if context is None:
        raise HTTPException(409, detail="no_browser_context")

    pages = context.pages
    if tab_index < 0 or tab_index >= len(pages):
        raise HTTPException(400, detail=f"tab_index out of range [0, {len(pages) - 1}]")

    new_page = pages[tab_index]
    if new_page.is_closed():
        raise HTTPException(409, detail="tab_is_closed")

    # Swap the active page
    info["page"] = new_page
    # Invalidate cached state for the old page
    invalidate_cdp_session(session_id)
    invalidate_viewport_cache(session_id)

    logger.info(
        "xioview.tab_switched",
        extra={
            "session_id": session_id,
            "tab_index": tab_index,
            "url": new_page.url,
        },
    )

    return {
        "session_id": session_id,
        "tab_index": tab_index,
        "url": new_page.url,
        "ok": True,
    }


# ── Attach / Detach ───────────────────────────────────────────────────────────


class AttachRequest(BaseModel):
    cdp_ws_url: str
    session_id: str | None = None
    internal_secret: str | None = None
    mode: str = "cdp_screencast"
    novnc_url: str | None = None  # direct noVNC URL on the worker (for HITL)
    profile_id: str | None = None  # PRFL-NNN for profile-aware routing
    worker_node: str | None = None  # xiogrid--default--worker-018


@public_router.post("/attach", summary="Attach XIOSYNC to an existing Colab Chrome CDP session")
async def attach_session(body: AttachRequest) -> dict[str, Any]:
    from xiosync.subsystems.xioview.session_manager import attach_browser  # noqa: PLC0415

    try:
        result = await attach_browser(
            cdp_ws_url=body.cdp_ws_url,
            session_id=body.session_id,
            mode=body.mode,
            internal_secret=body.internal_secret,
            novnc_url=body.novnc_url,
            profile_id=body.profile_id,
            worker_node=body.worker_node,
        )
        return result
    except ValueError as e:
        raise HTTPException(403, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(500, detail=str(e)) from e


@public_router.delete(
    "/attach/{session_id}", summary="Detach and close a manually-attached session"
)
async def detach_session(session_id: str) -> dict[str, Any]:
    from xiosync.subsystems.xioview.session_manager import detach_browser  # noqa: PLC0415

    return await detach_browser(session_id)


# ── Viewer HTML ────────────────────────────────────────────────────────────────


@public_router.get(
    "/sessions/{session_id}/view",
    response_class=HTMLResponse,
    summary="Browser viewer page for a session — HITL-aware, auto-embeds noVNC when active",
)
async def session_viewer(session_id: str, request: Request) -> HTMLResponse:
    from xiosync.subsystems.xioview.registry import get_registry  # noqa: PLC0415
    from xiosync.subsystems.xioview.viewer import render_viewer  # noqa: PLC0415

    host = request.headers.get("host", "localhost:8000")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    ws_url = f"{scheme}://{host}/api/v1/xioview/sessions/{session_id}/observe"

    # Resolve session metadata (novnc_url, profile_id) from registry
    reg = get_registry()
    entry = reg.get_entry(session_id)
    novnc_url = entry.novnc_url if entry else None
    profile_id = entry.profile_id if entry else None

    # Look for any pending HITL notice for this session (fetched from xiorun agent)
    hitl_notice: dict | None = None
    if novnc_url:
        # Try fetching pending HITL from the worker agent — derive worker base URL
        # from the novnc_url (e.g. http://100.111.130.118:6080 → http://100.111.130.118:9300)
        try:
            import json as _json
            import urllib.request as _ur

            _worker_ip = novnc_url.split("://")[1].split(":")[0]
            _hitl_url = f"http://{_worker_ip}:9300/hitl/pending"
            _resp = _ur.urlopen(_hitl_url, timeout=2)
            _data = _json.loads(_resp.read())
            _notices = _data.get("notices", [])
            # Find the most recent PENDING notice for this session
            _sess_notices = [
                n
                for n in _notices
                if n.get("state") == "PENDING" and n.get("session_id") == session_id
            ]
            if _sess_notices:
                hitl_notice = _sess_notices[-1]
                hitl_notice["novnc_url"] = novnc_url  # embed URL in notice for JS
                hitl_notice["resume_url"] = (
                    f"http://{_worker_ip}:9300/hitl/{hitl_notice['id']}/resume"
                )
        except Exception:
            pass  # HITL fetch is best-effort; don't fail the viewer

    return HTMLResponse(
        content=render_viewer(
            session_id=session_id,
            ws_url=ws_url,
            novnc_url=novnc_url,
            profile_id=profile_id,
            hitl_notice=hitl_notice,
        )
    )


# ── WebSocket Observation Endpoint ─────────────────────────────────────────────


@public_router.websocket("/sessions/{session_id}/observe")
async def observe_session(
    websocket: WebSocket,
    session_id: str,
    mode: str = Query(default=MODE_SCREENSHOT),
) -> None:
    """Live browser session observation + remote control WebSocket."""
    from xiosync.subsystems.xioview.session_manager import (  # noqa: PLC0415
        get_attached_info,
        get_playwright_page,
    )

    if mode not in VALID_MODES:
        await websocket.close(code=4400, reason=f"invalid_mode: {mode}")
        return

    await websocket.accept()
    org_id = _get_org_id(websocket)
    actor_id = ""
    ctx = getattr(websocket.state, "org_context", None)
    if ctx:
        actor_id = str(ctx.actor_id)
    reg = get_registry()
    queue: asyncio.Queue = asyncio.Queue(maxsize=30)

    logger.info(
        "xioview.client_connected",
        extra={
            "session_id": session_id,
            "org_id": org_id,
            "mode": mode,
        },
    )

    # Q-4: Audit log session observation
    from xiosync.subsystems.xioview.audit import get_audit_log  # noqa: PLC0415

    _audit = get_audit_log()
    _audit.record_session_start(session_id, org_id, actor_id, mode)

    # Track all background tasks for cleanup
    background_tasks: list[asyncio.Task] = []

    try:
        # Validate session (skip for manually-attached sessions)
        _is_attached = get_attached_info(session_id) is not None
        db = getattr(websocket.state, "org_session", None)
        if db and not _is_attached:
            try:
                _assert_session_visible(session_id, org_id, db)
            except HTTPException as e:
                await websocket.send_json({"type": MSG_ERROR, "detail": e.detail})
                await websocket.close(code=4403)
                return

        # Register client — starts screenshot capture if first client (screenshot mode only)
        _screenshot_getter = (
            (lambda: get_playwright_page(session_id)) if mode == MODE_SCREENSHOT else None
        )
        entry = reg.add_client(
            session_id=session_id,
            org_id=org_id,
            mode=mode,
            queue=queue,
            page_getter=_screenshot_getter,
        )

        # Send connected confirmation
        await websocket.send_json(
            {
                "type": MSG_CONNECTED,
                "session_id": session_id,
                "mode": mode,
                "fps": entry.fps,
            }
        )

        # Send last good frame immediately (screenshot mode)
        if entry.last_frame and mode == MODE_SCREENSHOT:
            import base64  # noqa: PLC0415

            await websocket.send_json(
                {
                    "type": MSG_FRAME,
                    "mode": "screenshot",
                    "jpeg_b64": base64.b64encode(entry.last_frame).decode(),
                }
            )

        # Push session_info — retry for up to 10s waiting for page
        try:
            _page = None
            for _retry in range(20):
                _page = await get_playwright_page(session_id)
                if _page is not None:
                    break
                await asyncio.sleep(0.5)
            if _page:
                try:
                    _vp = await _page.evaluate(
                        "() => ({ w: window.innerWidth, h: window.innerHeight })"
                    )
                    _sw = _vp.get("w", entry.stream_width)
                    _sh = _vp.get("h", entry.stream_height)
                    entry.viewport_width = _sw
                    entry.viewport_height = _sh
                    # Cache for coordinate scaling
                    from xiosync.subsystems.xioview.session_manager import (
                        cache_viewport,  # noqa: PLC0415
                    )

                    cache_viewport(session_id, _sw, _sh)
                except Exception:
                    _sw, _sh = entry.stream_width, entry.stream_height
                await websocket.send_json(
                    {
                        "type": MSG_SESSION_INFO,
                        "url": _page.url,
                        "width": _sw,
                        "height": _sh,
                    }
                )
        except Exception:
            pass

        # Start mode-specific background tasks
        if mode == MODE_CDP_SCREENCAST:
            from xiosync.subsystems.xioview.screencast import cdp_screencast_loop  # noqa: PLC0415

            t = asyncio.create_task(
                cdp_screencast_loop(session_id, queue),
                name=f"xioview-cdp-{session_id[:8]}",
            )
            background_tasks.append(t)

        elif mode == MODE_DOM_STREAM:
            # C-7: dom_stream injects rrweb JS into the page, which violates
            # stealth/anti-fingerprinting properties. Deprecated in favor of
            # cdp_dom_snapshot or dom_overlay which use only CDP commands.
            logger.warning(
                "xioview.dom_stream_deprecated",
                extra={
                    "session_id": session_id,
                    "detail": "dom_stream (rrweb) injects JS that violates stealth mode. "
                    "Use cdp_dom_snapshot or dom_overlay instead.",
                },
            )
            await websocket.send_json(
                {
                    "type": MSG_SESSION_INFO,
                    "deprecation": "dom_stream mode injects rrweb JS into the page, "
                    "which may trigger bot detection. Consider using "
                    "cdp_dom_snapshot or dom_overlay mode instead.",
                }
            )
            from xiosync.subsystems.xioview.screencast import inject_rrweb  # noqa: PLC0415

            t = asyncio.create_task(
                inject_rrweb(session_id, queue),
                name=f"xioview-rrweb-{session_id[:8]}",
            )
            background_tasks.append(t)

        elif mode == MODE_CDP_DOM_SNAPSHOT:
            from xiosync.subsystems.xioview.modes.cdp_dom_snapshot import (  # noqa: PLC0415
                cdp_dom_snapshot_loop,
            )

            t = asyncio.create_task(
                cdp_dom_snapshot_loop(
                    session_id,
                    queue,
                    get_page=lambda: get_playwright_page(session_id),
                ),
                name=f"xioview-domsnapshot-{session_id[:8]}",
            )
            background_tasks.append(t)

        elif mode == MODE_DOM_OVERLAY:
            from xiosync.subsystems.xioview.modes.dom_overlay import (  # noqa: PLC0415
                dom_overlay_loop,
            )
            from xiosync.subsystems.xioview.screencast import cdp_screencast_loop  # noqa: PLC0415

            t1 = asyncio.create_task(
                cdp_screencast_loop(session_id, queue),
                name=f"xioview-cdp-{session_id[:8]}",
            )
            t2 = asyncio.create_task(
                dom_overlay_loop(
                    session_id,
                    queue,
                    get_page=lambda: get_playwright_page(session_id),
                ),
                name=f"xioview-domoverlay-{session_id[:8]}",
            )
            background_tasks.extend([t1, t2])

        # Run sender + receiver concurrently
        send_task = asyncio.create_task(
            _sender(websocket, queue),
            name=f"xioview-sender-{session_id[:8]}",
        )
        recv_task = asyncio.create_task(
            _receiver(websocket, session_id, org_id),
            name=f"xioview-receiver-{session_id[:8]}",
        )

        done, pending = await asyncio.wait(
            [send_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "xioview.session_error",
            extra={
                "session_id": session_id,
                "error": str(exc),
            },
        )
    finally:
        # FIX (S-2): Cancel ALL background tasks on disconnect
        for t in background_tasks:
            if not t.done():
                t.cancel()
        reg.remove_client(session_id, queue)
        # Q-4: Audit log session end
        _audit.record_session_end(session_id, org_id, actor_id)
        logger.info("xioview.client_disconnected", extra={"session_id": session_id})


# ── Internal Coroutines ────────────────────────────────────────────────────────


async def _sender(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Pump queued messages to the WebSocket client.

    Sends keepalives every 30s. Also sends WebSocket ping frames for
    bidirectional health detection (Q-5 fix).
    """
    _keepalive = json.dumps({"type": MSG_KEEPALIVE})
    _missed_pongs = 0
    while True:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=30.0)
            await websocket.send_text(msg)
        except TimeoutError:
            await websocket.send_text(_keepalive)
            # WebSocket-level ping for connection health
            try:
                await websocket.send_bytes(b"")  # triggers pong
                _missed_pongs = 0
            except Exception:
                _missed_pongs += 1
                if _missed_pongs >= 3:
                    logger.info("xioview.client_no_pong", extra={"count": _missed_pongs})
                    break
        except (WebSocketDisconnect, RuntimeError):
            break


def _check_control_permission(websocket: WebSocket) -> bool:
    """Check if the connected client has session.control capability (Q-3 fix).

    Returns True if control is allowed. ORG_ADMINs and ORG_OWNERs can control;
    ORG_MEMBERs can only observe.
    """
    ctx = getattr(websocket.state, "org_context", None)
    if ctx is None:
        # No auth context (e.g., public router) — allow for backward compat
        # with internal test scripts using the internal_secret pattern.
        return True
    try:
        from xiosync.domain.context import MembershipRole  # noqa: PLC0415

        role = ctx.membership_role
        return role in (MembershipRole.ORG_ADMIN, MembershipRole.ORG_OWNER)
    except Exception:
        return False


# Control message types that require session.control RBAC
_RBAC_CONTROLLED_TYPES = frozenset(
    {
        "mouse_move",
        "mousedown",
        "mouseup",
        "click",
        "dblclick",
        "key",
        "type",
        "scroll",
    }
)


async def _receiver(
    websocket: WebSocket,
    session_id: str,
    org_id: str,
) -> None:
    """Receive and dispatch remote-control commands from the operator.

    Enforces session.control RBAC: interactive commands (mouse, key, scroll)
    require ORG_ADMIN or higher. Observation-only commands (set_fps) are
    allowed for any authenticated user.
    """
    from xiosync.subsystems.xioview.audit import get_audit_log  # noqa: PLC0415
    from xiosync.subsystems.xioview.control import dispatch_control  # noqa: PLC0415

    reg = get_registry()
    _can_control = _check_control_permission(websocket)
    _audit = get_audit_log()
    _actor_id = ""
    _ctx = getattr(websocket.state, "org_context", None)
    if _ctx:
        _actor_id = str(_ctx.actor_id)

    while True:
        try:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
        except (WebSocketDisconnect, RuntimeError):
            break
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type", "")

        # Q-3 fix: Enforce RBAC for interactive control
        if msg_type in _RBAC_CONTROLLED_TYPES and not _can_control:
            reg.push_event(
                session_id,
                {
                    "type": "interaction_blocked",
                    "reason": "insufficient_permission",
                    "detail": "Remote control requires ORG_ADMIN role or higher.",
                },
            )
            continue

        try:
            ack = await dispatch_control(msg_type, msg, session_id, org_id)
            if ack is not None:
                reg.push_event(session_id, ack)
                # Q-4: Audit control interaction
                _audit.record_control(
                    msg_type,
                    session_id,
                    org_id,
                    _actor_id,
                    detail={k: v for k, v in msg.items() if k != "type"},
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "xioview.control_error",
                extra={
                    "type": msg_type,
                    "error": str(exc),
                    "session_id": session_id,
                },
            )
