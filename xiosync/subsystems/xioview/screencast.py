"""XIOVIEW Screencast — CDP screencast streaming engine and rrweb DOM stream injector."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import pathlib
import struct
from typing import Any

from xiosync.subsystems.xioview import protocol

logger = logging.getLogger(__name__)


async def cdp_screencast_loop(session_id: str, queue: asyncio.Queue) -> None:
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
    from xiosync.subsystems.xioview.session_manager import (  # noqa: PLC0415
        cache_viewport,
        get_or_create_cdp_session,
        get_playwright_page,
    )

    # 1. Wait up to 15s for the page
    page = None
    for _wait_i in range(30):
        try:
            page = await get_playwright_page(session_id)
        except asyncio.CancelledError:
            return
        if page is not None:
            break
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    if page is None:
        logger.warning("xioview.cdp_no_page", extra={"session_id": session_id})
        return

    # 2. Get or create CDP session
    try:
        cdp = await get_or_create_cdp_session(session_id, page)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning(
            "xioview.cdp_session_failed", extra={"session_id": session_id, "error": str(exc)}
        )
        return

    # 3. Probe frame dimensions via Page.captureScreenshot
    STREAM_W, STREAM_H = 1920, 1080  # defaults
    try:
        _probe = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 30})
        _jpeg = base64.b64decode(_probe.get("data", ""))
        _i = 2
        while _i < len(_jpeg) - 4:
            if _jpeg[_i] != 0xFF:
                break
            if _jpeg[_i + 1] in (0xC0, 0xC2):
                STREAM_H = struct.unpack(">H", _jpeg[_i + 5 : _i + 7])[0]
                STREAM_W = struct.unpack(">H", _jpeg[_i + 7 : _i + 9])[0]
                break
            _i += 2 + struct.unpack(">H", _jpeg[_i + 2 : _i + 4])[0]
    except asyncio.CancelledError:
        return
    except Exception:
        pass

    # 4. Update stream dimensions in session_manager
    cache_viewport(session_id, STREAM_W, STREAM_H)

    # 5. Push session_info to queue
    try:
        queue.put_nowait(
            json.dumps(
                {
                    "type": protocol.MSG_SESSION_INFO,
                    "url": page.url,
                    "width": STREAM_W,
                    "height": STREAM_H,
                }
            )
        )
    except asyncio.QueueFull:
        pass

    # 6. Start Phase 1 screencast
    _frames: list[int] = [0]

    def on_frame(event: dict[str, Any]) -> None:
        data = event.get("data", "")
        frame_no = event.get("sessionId", 0)
        _frames[0] += 1
        try:
            queue.put_nowait(
                json.dumps(
                    {
                        "type": protocol.MSG_FRAME,
                        "mode": "cdp_screencast",
                        "jpeg_b64": data,
                    }
                )
            )
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
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 70,
                "maxWidth": STREAM_W,
                "maxHeight": STREAM_H,
                "everyNthFrame": 1,
            },
        )
        logger.info(
            "xioview.cdp_screencast_started",
            extra={"session_id": session_id, "res": f"{STREAM_W}x{STREAM_H}"},
        )
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning(
            "xioview.cdp_screencast_start_failed",
            extra={"session_id": session_id, "error": str(exc)},
        )

    # 7. Wait 3s for frames
    try:
        await asyncio.sleep(3.0)
    except asyncio.CancelledError:
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        return

    # 8. If <=3 frames, fall back to Phase 2 polling
    if _frames[0] > 3:
        # Genuine event-driven screencast working
        logger.info(
            "xioview.cdp_screencast_live", extra={"session_id": session_id, "frames_3s": _frames[0]}
        )
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
        return

    # Phase 2: no frames — fall back to polling Page.captureScreenshot
    logger.info(
        "xioview.cdp_screencast_poll_fallback",
        extra={
            "session_id": session_id,
            "reason": "0 frames in 3s — UC Chrome/Xvfb, falling back to poll",
        },
    )
    try:
        await cdp.send("Page.stopScreencast")
    except asyncio.CancelledError:
        return
    except Exception:
        pass

    POLL_INTERVAL = 0.13
    error_backoff = 0.5
    # Q-6: Adaptive JPEG quality on backpressure
    _quality = 70
    _consecutive_full = 0
    _QUALITY_TIERS = [70, 50, 30]  # degrade as backpressure increases
    try:
        while True:
            try:
                result = await cdp.send(
                    "Page.captureScreenshot",
                    {
                        "format": "jpeg",
                        "quality": _quality,
                        "fromSurface": True,
                        "captureBeyondViewport": False,
                    },
                )
                error_backoff = 0.5  # reset backoff on success
                jpeg_b64 = result.get("data", "")
                if jpeg_b64:
                    try:
                        queue.put_nowait(
                            json.dumps(
                                {
                                    "type": protocol.MSG_FRAME,
                                    "mode": "cdp_screencast",
                                    "jpeg_b64": jpeg_b64,
                                }
                            )
                        )
                        # Queue accepted frame — reduce backpressure counter
                        if _consecutive_full > 0:
                            _consecutive_full = max(0, _consecutive_full - 1)
                        _quality = _QUALITY_TIERS[min(_consecutive_full, len(_QUALITY_TIERS) - 1)]
                    except asyncio.QueueFull:
                        _consecutive_full += 1
                        _quality = _QUALITY_TIERS[min(_consecutive_full, len(_QUALITY_TIERS) - 1)]
                        # Notify client of frame drop
                        try:
                            # Drop oldest to make room for drop notification
                            queue.get_nowait()
                            queue.put_nowait(
                                json.dumps(
                                    {
                                        "type": "frame_dropped",
                                        "quality": _quality,
                                        "backpressure": _consecutive_full,
                                    }
                                )
                            )
                        except (asyncio.QueueFull, asyncio.QueueEmpty):
                            pass
                    _frames[0] += 1
                    # Refresh URL every ~10s
                    if _frames[0] % 30 == 0:
                        try:
                            queue.put_nowait(
                                json.dumps(
                                    {
                                        "type": protocol.MSG_SESSION_INFO,
                                        "url": page.url,
                                        "width": STREAM_W,
                                        "height": STREAM_H,
                                    }
                                )
                            )
                        except asyncio.QueueFull:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "xioview.poll_screenshot_error",
                    extra={"session_id": session_id, "error": str(exc), "backoff": error_backoff},
                )
                await asyncio.sleep(error_backoff)
                error_backoff = min(5.0, error_backoff * 2)
            else:
                await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        pass


_RRWEB_JS_PATH: str | None = None


def _rrweb_path() -> str | None:
    """Return path to vendored rrweb.min.js, or None if not present."""
    global _RRWEB_JS_PATH
    if _RRWEB_JS_PATH is not None:
        return _RRWEB_JS_PATH
    candidates = [
        pathlib.Path(__file__).parent.parent.parent.parent.parent
        / "tools"
        / "static"
        / "rrweb.min.js",
        pathlib.Path(__file__).parent / "static" / "rrweb.min.js",
    ]
    for p in candidates:
        if p.exists():
            _RRWEB_JS_PATH = str(p)
            return _RRWEB_JS_PATH
    return None


async def inject_rrweb(session_id: str, queue: asyncio.Queue) -> None:
    """Inject rrweb recorder into the page and bridge events to the WS queue.

    Falls back gracefully if rrweb.min.js is not yet vendored. The caller
    should download rrweb.min.js from https://github.com/rrweb-io/rrweb and
    place it at tools/static/rrweb.min.js.
    """
    from xiosync.subsystems.xioview.session_manager import get_playwright_page  # noqa: PLC0415

    logger.warning(
        "xioview.rrweb_violates_stealth",
        extra={"session_id": session_id, "reason": "rrweb injects JS that violates stealth mode"},
    )

    path = _rrweb_path()
    if path is None:
        msg = json.dumps(
            {
                "type": protocol.MSG_ERROR,
                "detail": "rrweb not vendored — place rrweb.min.js at tools/static/rrweb.min.js",
            }
        )
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass
        return

    page = await get_playwright_page(session_id)
    if page is None:
        return

    try:
        # expose_function bridges JS → Python synchronously (Playwright internals)
        async def _emit(event: Any) -> None:
            msg = json.dumps({"type": protocol.MSG_DOM_EVENT, "event": event})
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

        await page.expose_function("__xioview_emit", _emit)
        await page.add_init_script(path=path)
        await page.evaluate(
            "() => { if (typeof rrweb !== 'undefined') { rrweb.record({ emit: window.__xioview_emit }); } }"
        )
        logger.info("xioview.rrweb_injected", extra={"session_id": session_id})

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "xioview.rrweb_inject_failed", extra={"session_id": session_id, "error": str(exc)}
        )
