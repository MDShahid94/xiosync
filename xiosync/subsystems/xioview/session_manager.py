"""XIOVIEW Session Manager — manages the lifecycle of browser sessions.

This module is the authoritative owner of browser session state for XIOVIEW.
It manages manually-attached sessions and caches CDP sessions for interaction,
providing a clean API for other modules to access browser state.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from xiosync.subsystems.xioview.protocol import DOM_CURSOR_JS

logger = logging.getLogger(__name__)

# ── Module-level shared state ─────────────────────────────────────────────────

# session_id → {pw, browser, context, page, mode, stream_width, stream_height, _viewport_w, _viewport_h}
_attached_browsers: dict[str, dict[str, Any]] = {}

# session_id → (CDPSession, page) tuple
_interaction_cdp_sessions: dict[str, Any] = {}


# ── CDP Session Management ────────────────────────────────────────────────────

async def get_or_create_cdp_session(session_id: str, page: Any) -> Any:
    """Get or create a cached CDP session for dispatching interactions.

    Reuses the same CDP session across interactions to avoid the 1-3s overhead
    of a health check on every click/keystroke.

    The session is recreated when:
    - No cached session exists.
    - The cached session was created for a different page object (after navigation).
    """
    entry = _interaction_cdp_sessions.get(session_id)
    if entry is not None:
        cdp, cached_page = entry if isinstance(entry, tuple) else (entry, None)
        if cached_page is page:
            return cdp

        _interaction_cdp_sessions.pop(session_id, None)
        try:
            await cdp.detach()
        except Exception:
            pass

    cdp = await page.context.new_cdp_session(page)
    _interaction_cdp_sessions[session_id] = (cdp, page)
    logger.debug("xioview.cdp_session_created", extra={"session_id": session_id})
    return cdp


def invalidate_cdp_session(session_id: str) -> None:
    """Pop from _interaction_cdp_sessions and try to detach.

    Called by control.py on dispatch failure.
    """
    entry = _interaction_cdp_sessions.pop(session_id, None)
    if entry is not None:
        cdp = entry[0] if isinstance(entry, tuple) else entry
        try:
            asyncio.create_task(cdp.detach())
        except Exception:
            pass


# ── Browser Attachment ────────────────────────────────────────────────────────

async def get_playwright_page(session_id: str) -> Any | None:
    """Resolve the live Playwright page for a browser session.

    Three-registry lookup (priority order):
      1. _attached_browsers      — manually attached sessions
      2. run_dispatcher._active_pages — page currently mid-run
      3. XIORunRuntimePool._pages    — page alive between runs
    """
    attached = _attached_browsers.get(session_id)
    if attached:
        page = attached.get("page")
        if page and not page.is_closed():
            return page

    try:
        from xiosync.worker.run_dispatcher import get_active_page  # noqa: PLC0415
        page = await get_active_page(session_id)
        if page is not None:
            return page
    except Exception:
        pass

    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
        return get_runtime_pool().get_page(session_id)
    except Exception:
        return None


async def attach_browser(
    cdp_ws_url: str,
    session_id: str | None,
    mode: str,
    internal_secret: str | None,
    novnc_url: str | None = None,
    profile_id: str | None = None,
    worker_node: str | None = None,
) -> dict[str, Any]:
    """Connect XIOSYNC's patchright to a running Chrome via CDP URL."""
    expected = os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    if expected and internal_secret != expected:
        raise ValueError("invalid_internal_secret")

    from patchright.async_api import async_playwright  # noqa: PLC0415
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

    await cleanup_dead_sessions()

    session_id = session_id or str(uuid.uuid4())

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

    cdp_http_url = cdp_ws_url.replace("ws://", "http://").split("/json")[0]
    logger.info("xioview.attach_start", extra={
        "session_id": session_id,
        "url": cdp_http_url,
        "mode": mode,
        "novnc_url": novnc_url,
        "profile_id": profile_id,
    })

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp_http_url)
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            context = await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
    except Exception as exc:
        await pw.stop()
        raise RuntimeError(f"CDP attach failed: {exc}") from exc

    _attached_browsers[session_id] = {
        "pw": pw,
        "browser": browser,
        "context": context,
        "page": page,
        "mode": mode,
        "stream_width": 1920,
        "stream_height": 1080,
        "novnc_url": novnc_url,
        "profile_id": profile_id,
        "worker_node": worker_node,
    }

    get_runtime_pool()._register(session_id, page, None)

    # Update registry entry with profile/worker metadata for HITL-aware viewer
    try:
        from xiosync.subsystems.xioview.registry import get_registry  # noqa: PLC0415
        get_registry().update_session_meta(
            session_id, novnc_url=novnc_url, profile_id=profile_id, worker_node=worker_node,
        )
    except Exception:
        pass

    try:
        cdp = await page.context.new_cdp_session(page)
        _interaction_cdp_sessions[session_id] = (cdp, page)
        logger.info("xioview.cdp_session_warmed", extra={"session_id": session_id})

        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": DOM_CURSOR_JS})
        await cdp.send("Runtime.evaluate", {"expression": DOM_CURSOR_JS, "returnByValue": False})
        logger.info("xioview.dom_cursor_injected", extra={"session_id": session_id})

        async def _on_load(event_page: Any = None) -> None:
            invalidate_viewport_cache(session_id)
            try:
                cur_entry = _interaction_cdp_sessions.get(session_id)
                if cur_entry:
                    cur_cdp = cur_entry[0] if isinstance(cur_entry, tuple) else cur_entry
                    await cur_cdp.send(
                        "Runtime.evaluate",
                        {"expression": DOM_CURSOR_JS, "returnByValue": False}
                    )
            except Exception:
                pass

        page.on("load", _on_load)

    except Exception as warm_exc:
        logger.warning("xioview.cdp_warmup_failed", extra={
            "session_id": session_id,
            "error": str(warm_exc),
        })

    current_url = page.url
    logger.info("xioview.attached", extra={
        "session_id": session_id,
        "url": current_url,
        "mode": mode,
        "novnc_url": novnc_url,
        "profile_id": profile_id,
    })

    return {
        "session_id": session_id,
        "ok": True,
        "current_url": current_url,
        "view_url": f"/api/v1/xioview/sessions/{session_id}/view",
        "mode": mode,
        "novnc_url": novnc_url,
        "profile_id": profile_id,
        "worker_node": worker_node,
    }


async def detach_browser(session_id: str) -> dict[str, Any]:
    """Close the CDP connection for an attached session."""
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

    old = _attached_browsers.pop(session_id, None)
    entry = _interaction_cdp_sessions.pop(session_id, None)
    if entry is not None:
        cdp = entry[0] if isinstance(entry, tuple) else entry
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


async def cleanup_dead_sessions() -> None:
    """Remove _attached_browsers entries whose CDP connection has dropped."""
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
        logger.info("xioview.cleanup_dead_sessions", extra={
            "removed_count": len(dead),
            "alive_count": len(_attached_browsers),
        })


# ── Viewport and Stream Info Accessors ────────────────────────────────────────

def get_attached_info(session_id: str) -> dict[str, Any] | None:
    """Get the _attached_browsers entry for a session, if any."""
    return _attached_browsers.get(session_id)


def get_stream_dimensions(session_id: str) -> tuple[int, int]:
    """Read stream_width and stream_height for an attached session."""
    attached = _attached_browsers.get(session_id, {})
    return attached.get("stream_width", 1920), attached.get("stream_height", 1080)


def cache_viewport(session_id: str, width: int, height: int) -> None:
    """Store viewport and stream dimensions in the session's cache."""
    if session_id in _attached_browsers:
        _attached_browsers[session_id]["_viewport_w"] = width
        _attached_browsers[session_id]["_viewport_h"] = height
        _attached_browsers[session_id]["stream_width"] = width
        _attached_browsers[session_id]["stream_height"] = height


def invalidate_viewport_cache(session_id: str) -> None:
    """Pop _viewport_w and _viewport_h from a session's cache."""
    attached = _attached_browsers.get(session_id)
    if attached:
        attached.pop("_viewport_w", None)
        attached.pop("_viewport_h", None)


def get_cached_viewport(session_id: str) -> tuple[int | None, int | None]:
    """Return the cached viewport dimensions (width, height)."""
    attached = _attached_browsers.get(session_id, {})
    return attached.get("_viewport_w"), attached.get("_viewport_h")
