"""XIOVIEW remote browser control module.

Handles dispatching operator input events to remote Chrome via CDP,
coordinate scaling, and run-state checking.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from xiosync.subsystems.xioview.protocol import (
    CTL_CLICK,
    CTL_DBLCLICK,
    CTL_KEY,
    CTL_MOUSE_MOVE,
    CTL_MOUSEDOWN,
    CTL_MOUSEUP,
    CTL_PAUSE_WORKFLOW,
    CTL_RESUME_WORKFLOW,
    CTL_SCROLL,
    CTL_SET_FPS,
    CTL_TYPE,
)

logger = logging.getLogger(__name__)

# Track last mouse move per session for simple rate limiting
_last_move_ts: dict[str, float] = {}


async def scale_coords(
    session_id: str,
    page: Any,
    raw_x: int,
    raw_y: int,
) -> tuple[int, int]:
    """Scale viewer coordinates to actual Chrome viewport coordinates.

    The viewer sends coordinates relative to the stream dimensions (e.g.
    1920x1080 for CDP screencast). The actual Chrome viewport may differ.
    This function transforms client-space coords -> Chrome viewport-space coords.
    """
    from xiosync.subsystems.xioview.session_manager import get_attached_info  # noqa: PLC0415

    attached = get_attached_info(session_id)
    if attached is None:
        attached = {}

    stream_w = attached.get("stream_width", 1920)
    stream_h = attached.get("stream_height", 1080)

    actual_w = attached.get("_viewport_w")
    actual_h = attached.get("_viewport_h")

    if actual_w is None or actual_h is None:
        try:
            vp = await page.evaluate("() => ({ w: window.innerWidth, h: window.innerHeight })")
            actual_w = vp["w"]
            actual_h = vp["h"]
            if isinstance(attached, dict):
                attached["_viewport_w"] = actual_w
                attached["_viewport_h"] = actual_h
        except Exception:
            actual_w = stream_w
            actual_h = stream_h

    scaled_x = int(raw_x * actual_w / stream_w)
    scaled_y = int(raw_y * actual_h / stream_h)
    return max(0, scaled_x), max(0, scaled_y)


def _sync_check_db(session_id: str) -> bool:
    """Synchronous database check for active run."""
    try:
        from sqlalchemy import text  # noqa: PLC0415

        from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415

        engine = get_engine()
        if engine:
            with engine.connect() as conn:
                res = conn.execute(
                    text(
                        "SELECT 1 FROM xioflow_runs WHERE context->>'session_id' = :sid AND state = 'RUNNING'"
                    ),
                    {"sid": session_id},
                ).fetchone()
                if res:
                    return True
    except Exception:
        pass
    return False


def is_run_active(session_id: str) -> bool:
    """Check if any run (script OR DAG) is currently executing on this session.

    Delegates to runtime_pool.is_run_active() which tracks active runs for
    both execution paradigms. When a run is active, XIOVIEW blocks manual
    interactions to prevent conflicting input from corrupting execution.
    """
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

        if get_runtime_pool().is_run_active(session_id):
            return True
    except Exception:
        pass

    # TODO: The synchronous DB query blocks the event loop.
    # For now keep it sync here, but we wrap it in run_in_executor for async paths.
    # This should be fully async with async SQLAlchemy in Phase 2.
    return _sync_check_db(session_id)


async def is_run_active_async(session_id: str) -> bool:
    """Asynchronous version of is_run_active.

    Checks process-local cache first, then wraps the sync DB query in
    run_in_executor to avoid blocking the event loop.
    """
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

        if get_runtime_pool().is_run_active(session_id):
            return True
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_check_db, session_id)


async def set_run_state(
    run_id: str, org_id: str, new_state: str, valid_from: tuple[str, ...]
) -> None:
    """Directly transition a workflow run state - used by HITL pause/resume from XIOVIEW.

    Bypasses the HTTP self-call pattern; writes directly to xioflow_runs using the
    application engine under the correct org RLS context. Best-effort: any error
    is swallowed so the WebSocket session is never torn down by a pause/resume failure.
    """
    try:
        from sqlalchemy import text as _text  # noqa: PLC0415

        from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415

        engine = get_engine()
        if engine is None:
            logger.warning(
                "xioview.hitl_no_engine", extra={"detail": "engine_ref not yet initialized"}
            )
            return

        loop = asyncio.get_running_loop()

        def _write() -> None:
            from sqlalchemy.orm import Session as _Session  # noqa: PLC0415

            with _Session(engine) as s, s.begin():
                s.execute(
                    _text("SELECT set_config('app.current_org', :org, true)"), {"org": org_id}
                )
                result = s.execute(
                    _text("""
                    UPDATE xioflow_runs
                    SET    state = :new_state
                    WHERE  id    = :id
                      AND  organization_id = :org
                      AND  state = ANY(:valid_from)
                """),
                    {
                        "new_state": new_state,
                        "id": run_id,
                        "org": org_id,
                        "valid_from": list(valid_from),
                    },
                )
                if result.rowcount == 0:
                    logger.warning(
                        "xioview.hitl_run_not_found_or_wrong_state",
                        extra={
                            "run_id": run_id,
                            "new_state": new_state,
                        },
                    )

        await loop.run_in_executor(None, _write)
        logger.info(
            "xioview.hitl_state_set",
            extra={
                "run_id": run_id,
                "new_state": new_state,
                "org_id": org_id,
            },
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "xioview.hitl_set_run_state_failed",
            extra={
                "run_id": run_id,
                "error": str(exc),
            },
        )


async def dispatch_control(
    msg_type: str, msg: dict[str, Any], session_id: str, org_id: str
) -> dict[str, Any] | None:
    """Route incoming operator commands to remote Chrome via raw CDP.

    Uses CDP Input.dispatchMouseEvent / Input.dispatchKeyEvent for isTrusted=true
    events that pass bot detection, matching the pattern used by xiorun_agent.py.
    """
    if msg_type == CTL_PAUSE_WORKFLOW:
        run_id = msg.get("run_id")
        if run_id:
            await set_run_state(run_id, org_id, "PAUSED", ("RUNNING", "PENDING"))
        return None
    elif msg_type == CTL_RESUME_WORKFLOW:
        run_id = msg.get("run_id")
        if run_id:
            await set_run_state(run_id, org_id, "PENDING", ("PAUSED",))
        return None
    elif msg_type == CTL_SET_FPS:
        fps = float(msg.get("fps", 5.0))
        from xiosync.subsystems.xioview.registry import get_registry  # noqa: PLC0415

        reg = get_registry()
        entry = reg._sessions.get(session_id)
        if entry:
            entry.fps = max(0.1, min(15.0, fps))
        return None

    from xiosync.subsystems.xioview.session_manager import (  # noqa: PLC0415
        get_or_create_cdp_session,
        get_playwright_page,
        invalidate_cdp_session,
    )

    page = await get_playwright_page(session_id)
    if page is None:
        logger.warning("xioview.no_page_for_control", extra={"session_id": session_id})
        return None

    if msg_type in (
        CTL_CLICK,
        CTL_MOUSEDOWN,
        CTL_MOUSEUP,
        CTL_DBLCLICK,
        CTL_TYPE,
        CTL_KEY,
        CTL_SCROLL,
        CTL_MOUSE_MOVE,
    ) and await is_run_active_async(session_id):
        return {
            "type": "interaction_blocked",
            "reason": "run_active",
            "detail": "Interactions are blocked while a workflow is executing. "
            "Pause or wait for the run to complete to interact manually.",
        }

    # Bug fix S-6: Rate limiter for mouse_move
    if msg_type == CTL_MOUSE_MOVE:
        now = time.monotonic()
        last_time = _last_move_ts.get(session_id, 0.0)
        if now - last_time < 0.025:  # 25ms
            return None
        _last_move_ts[session_id] = now

    ack_event: dict[str, Any] | None = None

    for attempt in range(2):
        try:
            if msg_type == CTL_MOUSE_MOVE:
                x, y = await scale_coords(session_id, page, msg.get("x", 0), msg.get("y", 0))
                cdp = await get_or_create_cdp_session(session_id, page)
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseMoved",
                        "x": x,
                        "y": y,
                    },
                )
                await cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            f"(function(){{var e=document.getElementById('__xio_cur');"
                            f"if(e){{e.style.left='{x}px';e.style.top='{y}px';}}}})()"
                        ),
                        "returnByValue": False,
                    },
                )
                break

            elif msg_type == CTL_MOUSEDOWN:
                x, y = await scale_coords(session_id, page, msg.get("x", 0), msg.get("y", 0))
                cdp = await get_or_create_cdp_session(session_id, page)
                button = msg.get("button", "left")
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "button": button,
                        "clickCount": 1,
                        "x": x,
                        "y": y,
                    },
                )
                ack_event = {
                    "type": "interaction_ack",
                    "action": "mousedown",
                    "x": msg.get("x", 0),
                    "y": msg.get("y", 0),
                    "success": True,
                }
                break

            elif msg_type == CTL_MOUSEUP:
                x, y = await scale_coords(session_id, page, msg.get("x", 0), msg.get("y", 0))
                cdp = await get_or_create_cdp_session(session_id, page)
                button = msg.get("button", "left")
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "button": button,
                        "clickCount": 1,
                        "x": x,
                        "y": y,
                    },
                )
                ack_event = {
                    "type": "interaction_ack",
                    "action": "mouseup",
                    "x": msg.get("x", 0),
                    "y": msg.get("y", 0),
                    "success": True,
                }
                break

            elif msg_type == CTL_CLICK:
                x, y = await scale_coords(session_id, page, msg.get("x", 0), msg.get("y", 0))
                cdp = await get_or_create_cdp_session(session_id, page)
                button = msg.get("button", "left")
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "button": button,
                        "clickCount": 1,
                        "x": x,
                        "y": y,
                    },
                )
                await asyncio.sleep(0.05)
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "button": button,
                        "clickCount": 1,
                        "x": x,
                        "y": y,
                    },
                )
                ack_event = {
                    "type": "interaction_ack",
                    "action": "click",
                    "x": msg.get("x", 0),
                    "y": msg.get("y", 0),
                    "success": True,
                }
                break

            elif msg_type == CTL_DBLCLICK:
                x, y = await scale_coords(session_id, page, msg.get("x", 0), msg.get("y", 0))
                cdp = await get_or_create_cdp_session(session_id, page)
                for click_count in (1, 2):
                    await cdp.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mousePressed",
                            "button": "left",
                            "clickCount": click_count,
                            "x": x,
                            "y": y,
                        },
                    )
                    await asyncio.sleep(0.04)
                    await cdp.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseReleased",
                            "button": "left",
                            "clickCount": click_count,
                            "x": x,
                            "y": y,
                        },
                    )
                    if click_count == 1:
                        await asyncio.sleep(0.08)
                ack_event = {
                    "type": "interaction_ack",
                    "action": "dblclick",
                    "x": msg.get("x", 0),
                    "y": msg.get("y", 0),
                    "success": True,
                }
                break

            elif msg_type == CTL_KEY:
                key = msg.get("key", "")
                mods = msg.get("modifiers", [])
                for mod in mods:
                    await page.keyboard.down(mod)
                await page.keyboard.press(key)
                for mod in reversed(mods):
                    await page.keyboard.up(mod)
                ack_event = {
                    "type": "interaction_ack",
                    "action": "key",
                    "key": key,
                    "success": True,
                }
                break

            elif msg_type == CTL_TYPE:
                text = msg.get("text", "")
                await page.keyboard.type(text, delay=20)
                ack_event = {
                    "type": "interaction_ack",
                    "action": "type",
                    "text_len": len(text),
                    "success": True,
                }
                break

            elif msg_type == CTL_SCROLL:
                cdp = await get_or_create_cdp_session(session_id, page)
                # Bug fix S-4: Use coordinates from message instead of hardcoded 960, 540
                x = msg.get("x", 960)
                y = msg.get("y", 540)
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": msg.get("deltaX", 0),
                        "deltaY": msg.get("deltaY", 0),
                    },
                )
                break

            else:
                break

        except Exception as exc:
            invalidate_cdp_session(session_id)
            if attempt == 0:
                logger.debug(
                    "xioview.dispatch_retry",
                    extra={"session_id": session_id, "msg_type": msg_type, "error": str(exc)},
                )
                continue
            logger.warning(
                "xioview.control_dispatch_failed",
                extra={
                    "session_id": session_id,
                    "msg_type": msg_type,
                    "error": str(exc),
                },
            )
            ack_event = {
                "type": "interaction_ack",
                "action": msg_type,
                "success": False,
                "error": str(exc),
            }

    return ack_event
