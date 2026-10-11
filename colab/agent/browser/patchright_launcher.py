"""colab.agent.browser.patchright_launcher — Patchright Chromium lifecycle and browser endpoints.

Handles browser launch, termination, session listing, cookie injection, navigation,
health checks, and stealth JS debugging.
"""

from __future__ import annotations

import asyncio
import glob as _glob
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import PlainTextResponse

from colab.agent.browser.capacity import detect_runtime_capacity
from colab.agent.config import (
    CHROMIUM_ARGS,
    DRIVE_ROOT,
    NODE_NAME,
    XIOSYNC_BASE,
    XIOSYNC_TOKEN,
    _CHROME_VERSION,
    _NOVNC_URL,
    _REAL_CHROME,
    _SSH_PROXY_URL,
    _STEALTH_UA_CHROME,
)
from colab.agent.models import (
    InjectCookiesRequest,
    LaunchRequest,
    NavigateRequest,
    TerminateRequest,
)
from colab.agent.network.geo import _get_runtime_public_ip, _my_tailscale_ip, _resolve_ip_geo
from colab.agent.network.proxy_guard import _proxy_probe_loop
from colab.agent.state import _sessions, _sessions_lock, _uc_drivers
from colab.agent.stealth.fingerprint import _load_profile_fingerprint
from colab.agent.stealth.stealth_js import _STEALTH_JS

logger = logging.getLogger("xiorun_agent.browser.patchright_launcher")

# Runtime capacity and session limit
_RUNTIME_CAP = detect_runtime_capacity()
_MAX_SESSIONS: int = _RUNTIME_CAP["max_sessions"]


def _find_patchright_chromium() -> str | None:
    """Search the ms-patchright cache for the newest installed Chromium binary."""
    candidates = sorted(
        _glob.glob("/root/.cache/ms-patchright/chromium-*/chrome-linux64/chrome"),
        reverse=True,
    )
    return candidates[0] if candidates else None


_PATCHRIGHT_CHROME = _find_patchright_chromium()

# Use real Chrome if available; fall back to patchright; then auto-detect
CHROME_EXECUTABLE: str | None = (
    _REAL_CHROME
    if os.path.isfile(_REAL_CHROME)
    else _PATCHRIGHT_CHROME
    if _PATCHRIGHT_CHROME and os.path.isfile(_PATCHRIGHT_CHROME)
    else None  # let patchright auto-detect via PLAYWRIGHT_BROWSERS_PATH
)


# ── Endpoints ──────────────────────────────────────────────────────────────────
async def health() -> dict:
    _checks: dict[str, bool | str] = {}

    # ── Chrome binary check ──────────────────────────────────────────────
    _chrome_bin = _find_patchright_chromium()
    _checks["chrome_binary"] = bool(_chrome_bin)

    # ── Drive FUSE mount check ───────────────────────────────────────────
    try:
        _drive = Path(DRIVE_ROOT)
        _checks["drive_mounted"] = _drive.is_dir() and any(_drive.iterdir())
    except Exception:
        _checks["drive_mounted"] = False

    # ── XIOSYNC server reachability ──────────────────────────────────────
    _xio_base = os.environ.get("XIORUN_XIOSYNC_BASE", "")
    if _xio_base:
        try:
            import httpx as _hx_h

            _r = _hx_h.get(f"{_xio_base}/live", timeout=5)
            _checks["xiosync_reachable"] = _r.status_code == 200
        except Exception:
            _checks["xiosync_reachable"] = False
    else:
        _checks["xiosync_reachable"] = "not_configured"

    # ── Proxy check (SSH SOCKS5 or WS bridge) ────────────────────────────
    _checks["ssh_proxy"] = bool(_SSH_PROXY_URL)

    # Overall status: ok if Chrome is present
    _overall = _checks.get("chrome_binary", False) is True

    return {
        "ok": _overall,
        "node": NODE_NAME,
        "active_sessions": len(_sessions),
        "max_sessions": _MAX_SESSIONS,
        "available": max(0, _MAX_SESSIONS - len(_sessions)),
        "runtime_type": _RUNTIME_CAP["runtime_type"],
        "sys_ram_gb": _RUNTIME_CAP["sys_ram_gb"],
        "gpu_vram_gb": _RUNTIME_CAP["gpu_vram_gb"],
        "cpu_count": _RUNTIME_CAP["cpu_count"],
        "ssh_proxy_url": _SSH_PROXY_URL,
        "novnc_url": _NOVNC_URL,
        "checks": _checks,
    }


async def debug_stealth_js() -> str:
    """Return the fully-rendered stealth init script for the active PRFL session.

    Used by stealth_audit.py (run from Mac Mini via connect_over_cdp) to inject
    the same 22-section stealth JS into audit pages so WebGL renderer, canvas noise,
    audio context and navigator.platform all match the live profile fingerprint.
    """
    # First try: use the stealth JS cached from the last launched session
    for _sid, _info in _sessions.items():
        _js = _info.get("stealth_js") or _info.get("session_stealth_js")
        if _js and len(_js) > 500:
            return _js

    # Fallback: build from PRFL-002 fingerprint
    _fp_fb: dict = {}
    for _prfl_id in ["PRFL-002", "PRFL-001"]:
        try:
            _fp_fb = _load_profile_fingerprint(_prfl_id, DRIVE_ROOT) or {}
            if _fp_fb:
                break
        except Exception:
            pass

    _webgl_r = _fp_fb.get("webgl_renderer", "ANGLE (Apple, Apple M1, OpenGL 4.1)")
    _webgl_v = (
        "Apple"
        if "Apple" in _webgl_r
        else "Google Inc. (NVIDIA)"
        if "NVIDIA" in _webgl_r
        else "Google Inc. (Intel)"
    )
    _tz_fb = _fp_fb.get("timezone", "America/New_York")
    try:
        import datetime as _dtfb
        import zoneinfo as _zifb

        _tz_off_fb = -int(
            _dtfb.datetime.now(_zifb.ZoneInfo(_tz_fb)).utcoffset().total_seconds() // 60
        )
    except Exception:
        _tz_off_fb = 300
    _js_out = (
        _STEALTH_JS.replace("{WEBGL_V}", _webgl_v)
        .replace("{WEBGL_R}", _webgl_r)
        .replace("{UA}", _fp_fb.get("ua_template", _STEALTH_UA_CHROME))
        .replace("{PLATFORM}", _fp_fb.get("platform", "Win32"))
        .replace("{TIMEZONE}", _tz_fb)
        .replace("{TZ_OFFSET}", str(_tz_off_fb))
        .replace("{CANVAS_SEED}", str(_fp_fb.get("canvas_seed", 42)))
        .replace("{AUDIO_SEED}", str(_fp_fb.get("audio_seed", 7)))
        .replace("{SCREEN_W}", str(_fp_fb.get("width", 1920)))
        .replace("{SCREEN_H}", str(_fp_fb.get("height", 1080)))
        .replace("{CORES}", str(_fp_fb.get("cores", 8)))
        .replace("{RAM}", str(_fp_fb.get("ram", 16)))
        .replace("'{LOCALE}'", "'en-US'")
        .replace("{LOCALE}", "en-US")
        .replace("{LANG}", "en-US")
        .replace("{CH_PLATFORM}", _fp_fb.get("ch_platform", "macOS"))
        .replace("{CH_ARCH}", _fp_fb.get("ch_arch", "arm"))
        .replace("{LAT}", str(_fp_fb.get("lat", 40.7128)))
        .replace("{LON}", str(_fp_fb.get("lon", -74.0060)))
        .replace("{IS_MOBILE}", "false")
        .replace("{SCREEN_AH}", str(_fp_fb.get("height", 1080)))
        .replace("{UA_TEMPLATE}", _fp_fb.get("ua_template", _STEALTH_UA_CHROME))
    )
    return _js_out


async def list_sessions() -> dict:
    return {
        "sessions": [
            {
                "session_id": sid,
                "cdp_ws_url": info["cdp_ws_url"],
                "pid": info["pid"],
            }
            for sid, info in _sessions.items()
        ]
    }


async def launch_browser(req: LaunchRequest) -> dict:
    """Launch Chrome for a session via subprocess (Strategy A) or patchright (Strategy B fallback).

    Strategy A — real google-chrome-stable via subprocess + optional patchright CDP overlay.
      Preferred: no HeadlessChrome UA, undetectable. patchright is optional — if not installed,
      Chrome runs subprocess-only and UC login manages it via /run-uc-login.

    Strategy B — patchright managed headless-shell (fallback when no real Chrome binary).
    """
    # NOTE: patchright import intentionally deferred — Strategy A works without it.

    # ── Atomic check: idempotency + capacity gate under lock ──────────────
    async with _sessions_lock:
        if req.session_id in _sessions:
            info = _sessions[req.session_id]
            if not info.get("_reserving"):
                return {"cdp_ws_url": info["cdp_ws_url"], "pid": info["pid"], "port": info["port"]}
            # Another request is already launching this session — wait briefly
            raise HTTPException(status_code=409, detail="session_launch_in_progress")

        active = len(_sessions)
        if active >= _MAX_SESSIONS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Runtime at capacity: {active}/{_MAX_SESSIONS} sessions active "
                    f"({_RUNTIME_CAP['runtime_type']} runtime, "
                    f"{_RUNTIME_CAP['sys_ram_gb']} GB sys, "
                    f"{_RUNTIME_CAP['gpu_vram_gb']} GB GPU). "
                    "Terminate an existing session or use a different runtime."
                ),
            )
        # Reserve slot atomically — concurrent launches can't both pass capacity gate
        _sessions[req.session_id] = {"_reserving": True}

    # ── Resolve full geo profile from exit node's public IP ───────────────────
    _exit_ip = req.exit_node_public_ip
    if not _exit_ip:
        _exit_ip = await asyncio.get_event_loop().run_in_executor(None, _get_runtime_public_ip)
    _geo = await asyncio.get_event_loop().run_in_executor(None, _resolve_ip_geo, _exit_ip)
    _timezone = _geo["timezone"]
    _locale = _geo["locale"]
    _geo_lat = _geo["lat"]
    _geo_lon = _geo["lon"]
    logger.info(
        f"launch timezone={_timezone} locale={_locale} exit_ip={_exit_ip} session={req.session_id}"
    )

    profile_dir = req.profile_dir

    # ── Build per-session stealth JS (inject dynamic values) ─────────────────
    # If this session uses a named profile (PRFL-NNN), auto-load its persistent
    # fingerprint from Drive as the baseline. Caller-supplied fingerprint keys
    # always take precedence (dynamic per-session override still fully works).
    _fp = dict(req.fingerprint) if req.fingerprint else {}
    if profile_dir:
        import re as _re_prfl

        _prfl_match = _re_prfl.search(r"(PRFL-\d+)", profile_dir)
        if _prfl_match:
            _prfl_id = _prfl_match.group(1)
            _base_fp = _load_profile_fingerprint(_prfl_id, DRIVE_ROOT)
            # Merge: profile preset is base, caller's explicit keys override
            _merged = dict(_base_fp)
            _merged.update(_fp)
            _fp = _merged
            logger.info(
                f"launch: auto-loaded fingerprint for {_prfl_id}: "
                f"ua={_fp.get('ua_template', '')[:40]}..."
            )

    _canvas_seed = _fp.get("canvas_seed", 0x1A2B)

    _audio_seed = _fp.get("audio_seed", 0x3C4D)
    _cores = str(_fp.get("cores", os.cpu_count() or 2))
    _ram = str(_fp.get("ram", max(2, min(8, (os.cpu_count() or 2) * 2))))
    _platform = _fp.get("platform", "Win32")
    _ch_platform = _fp.get("ch_platform", "Windows")
    _ch_arch = _fp.get("ch_arch", "x86")
    _is_mobile = "true" if _fp.get("is_mobile") else "false"
    _screen_w = str(_fp.get("width", 1920))
    _screen_h = str(_fp.get("height", 1080))
    _screen_ah = str(max(100, int(_screen_h) - 40))
    _webgl_v = "Google Inc. (NVIDIA)"
    _webgl_r = _fp.get(
        "webgl_renderer", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    )
    if "Apple" in _webgl_r:
        _webgl_v = "Apple"
    elif "Intel" in _webgl_r:
        _webgl_v = "Google Inc. (Intel)"
    elif "AMD" in _webgl_r:
        _webgl_v = "Google Inc. (AMD)"
    _ua_str = _fp.get("ua_template", _STEALTH_UA_CHROME)

    # Compute TZ offset: minutes west of UTC (e.g. IST UTC+5:30 → -330)
    import datetime as _dt

    try:
        import zoneinfo as _zi

        _tz_obj = _zi.ZoneInfo(_timezone)
        _tz_offset = -int(_dt.datetime.now(_tz_obj).utcoffset().total_seconds() // 60)
    except Exception:
        _tz_offset = 0
    _session_stealth_js = (
        _STEALTH_JS.replace("'{TIMEZONE}'", f"'{_timezone}'")
        .replace("{TIMEZONE}", _timezone)
        .replace("'{LOCALE}'", f"'{_locale}'")
        .replace("{LOCALE}", _locale)
        .replace("{CANVAS_SEED}", str(_canvas_seed))
        .replace("{AUDIO_SEED}", str(_audio_seed))
        .replace("{LAT}", str(_geo_lat))
        .replace("{LON}", str(_geo_lon))
        .replace("'{UA}'", f"'{_ua_str}'")
        .replace("{UA}", _ua_str)
        .replace("{TZ_OFFSET}", str(_tz_offset))
        .replace("{CORES}", _cores)
        .replace("{RAM}", _ram)
        .replace("{PLATFORM}", _platform)
        .replace("{CH_PLATFORM}", _ch_platform)
        .replace("{CH_ARCH}", _ch_arch)
        .replace("{IS_MOBILE}", _is_mobile)
        .replace("{SCREEN_W}", _screen_w)
        .replace("{SCREEN_H}", _screen_h)
        .replace("{SCREEN_AH}", _screen_ah)
        .replace("{WEBGL_V}", _webgl_v)
        .replace("{WEBGL_R}", _webgl_r)
    )

    # Pre-allocate a free port
    import socket as _sock

    with _sock.socket() as _s:
        _s.bind(("", 0))
        port = _s.getsockname()[1]

    # Build Chrome args
    args = [a for a in CHROMIUM_ARGS if not a.startswith("--user-data-dir")]
    if req.headless:  # Only use headless when explicitly requested
        args.append("--headless=new")
    if hasattr(req, "pac_url") and req.pac_url:
        args.append(f"--proxy-pac-url={req.pac_url}")
    elif req.proxy_url:
        args.append(f"--proxy-server={req.proxy_url}")
    if profile_dir:
        args.append(f"--user-data-dir={profile_dir}")
    args.append(f"--remote-debugging-port={port}")

    tailscale_ip = _my_tailscale_ip()
    cdp_http_url = f"http://localhost:{port}"
    cdp_ws_url = f"ws://{tailscale_ip}:{port}"

    pw = None
    browser = None
    context = None
    chrome_proc = None

    # ── Strategy A: Subprocess launch of real Chrome ──────────────────────────
    if CHROME_EXECUTABLE:
        logger.info(f"chrome_launch=subprocess binary={CHROME_EXECUTABLE} headless={req.headless}")
        import subprocess as _sp

        env = dict(os.environ)
        env.setdefault("DISPLAY", ":99")
        chrome_proc = _sp.Popen(
            [CHROME_EXECUTABLE] + args,
            env=env,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
        )
        # Wait for CDP to become available (up to 15 s)
        import urllib.request as _ur

        for _i in range(30):
            await asyncio.sleep(0.5)
            try:
                _ur.urlopen(f"{cdp_http_url}/json/version", timeout=1)
                logger.info(f"chrome_ready port={port} attempt={_i}")
                break
            except Exception:
                pass
        else:
            chrome_proc.kill()
            raise HTTPException(status_code=500, detail="Chrome CDP did not become ready")

        # Try to attach patchright CDP overlay (optional — UC login works without it)
        try:
            from patchright.async_api import async_playwright as _aap

            pw = await _aap().start()
            browser = await pw.chromium.connect_over_cdp(cdp_http_url)
            context = (
                browser.contexts[0]
                if browser.contexts
                else await browser.new_context(user_agent=_STEALTH_UA_CHROME)
            )
            logger.info(f"patchright_cdp_overlay attached session={req.session_id}")
        except Exception as _pw_err:
            # patchright not installed — Chrome runs subprocess-only.
            # /run-uc-login will launch its own UC Chrome instance for the actual login.
            logger.info(f"patchright_optional_skip session={req.session_id} reason={_pw_err}")
            pw = None

        pid = chrome_proc.pid

    else:
        # ── Strategy B: patchright managed launch (fallback — headless-shell only) ──
        logger.info(f"chrome_launch=patchright headless={req.headless}")
        try:
            from patchright.async_api import async_playwright as _aap

            pw = await _aap().start()
        except ImportError as exc:
            raise HTTPException(
                status_code=500,
                detail="No real Chrome binary and patchright not installed. "
                "Install google-chrome-stable or patchright.",
            ) from exc
        pw_args = [a for a in args if not a.startswith("--user-data-dir")]
        try:
            if profile_dir:
                context = await pw.chromium.launch_persistent_context(
                    profile_dir,
                    headless=req.headless,
                    args=pw_args,
                    user_agent=_STEALTH_UA_CHROME,
                )
            else:
                browser = await pw.chromium.launch(headless=req.headless, args=pw_args)
        except Exception as exc:
            await pw.stop()
            raise HTTPException(status_code=500, detail=f"Browser launch failed: {exc}") from exc
        try:
            _impl = context._impl_obj if context else browser._impl_obj
            pid = getattr(getattr(_impl, "_browser", _impl), "_pid", 0)
        except Exception:
            pid = 0

    _sessions[req.session_id] = {
        "browser": browser,
        "context": context,
        "pw": pw,
        "pid": pid,
        "port": port,
        "cdp_ws_url": cdp_ws_url,
        "proxy_url": req.proxy_url,
        "profile_dir": profile_dir,
        "chrome_proc": chrome_proc,
        "guard_task": None,
        "timezone": _timezone,
        "exit_node_ip": _exit_ip,
        "stealth_js": _session_stealth_js,  # cached for /debug/stealth-js audit endpoint
    }

    # ── Apply stealth via HTML network interception (context.route) ──────────
    # NOTE: add_init_script() runs in Playwright's Isolated World and cannot
    # spoof WebGL/Screen/GPU APIs visible to page JS. We intercept HTML at the
    # network layer instead — stealth JS runs in the Main World before any site JS.
    try:
        _ctx = context or (browser.contexts[0] if browser and browser.contexts else None)
        if _ctx and _session_stealth_js:
            _inject_tag = f"<script>{_session_stealth_js}</script>"
            import re as _re_stealth

            async def _xiorun_stealth_handler(route):
                try:
                    resp = await route.fetch()
                    ct = resp.headers.get("content-type", "")
                    if "text/html" in ct:
                        body = await resp.text()
                        injected = False
                        for marker in ("<head>", "<HEAD>"):
                            if marker in body:
                                body = body.replace(marker, marker + _inject_tag, 1)
                                injected = True
                                break
                        if not injected:
                            m = _re_stealth.search(r"(<head[^>]*>)", body, _re_stealth.IGNORECASE)
                            if m:
                                body = body[: m.end()] + _inject_tag + body[m.end() :]
                            elif _re_stealth.search(r"<html[^>]*>", body, _re_stealth.IGNORECASE):
                                body = _re_stealth.sub(
                                    r"(<html[^>]*>)",
                                    r"\1" + _inject_tag,
                                    body,
                                    count=1,
                                )
                            else:
                                body = _inject_tag + body
                        await route.fulfill(
                            status=resp.status,
                            headers=dict(resp.headers),
                            body=body,
                        )
                    else:
                        await route.fulfill(response=resp)
                except Exception:
                    await route.continue_()

            await _ctx.route("**/*", _xiorun_stealth_handler)
            logger.info(
                f"stealth_route_applied session={req.session_id} tz={_timezone} script_bytes={len(_session_stealth_js)}"
            )
    except Exception as _se:
        logger.warning(f"stealth_route_failed session={req.session_id} err={_se}")

    # ── Crash watcher ─────────────────────────────────────────────────────────
    async def _on_browser_disconnected() -> None:
        if req.session_id not in _sessions:
            return
        _sessions.pop(req.session_id, None)
        logger.error(f"browser_crashed session={req.session_id}")
        if XIOSYNC_BASE and XIOSYNC_TOKEN:
            try:
                import httpx

                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        f"{XIOSYNC_BASE}/api/v1/internal/xiorun/browser-crashed",
                        json={
                            "session_id": req.session_id,
                            "node": NODE_NAME,
                            "reason": "browser_disconnected",
                        },
                        headers={"Authorization": f"Bearer {XIOSYNC_TOKEN}"},
                    )
            except Exception as exc:
                logger.warning(f"browser-crashed notify failed: {exc}")

    _watch_target = (
        browser if browser else (context.browser if context and context.browser else None)
    )
    if _watch_target:
        _watch_target.on("disconnected", lambda: asyncio.create_task(_on_browser_disconnected()))

    if req.proxy_url:
        task = asyncio.create_task(
            _proxy_probe_loop(req.session_id, req.proxy_url),
            name=f"proxy-guard-{req.session_id[:8]}",
        )
        _sessions[req.session_id]["guard_task"] = task

    logger.info(
        f"launched session={req.session_id} port={port} profile={profile_dir} cdp={cdp_ws_url}"
    )
    return {"cdp_ws_url": cdp_ws_url, "pid": pid, "port": port}


async def terminate_browser(req: TerminateRequest) -> dict:
    """Kill the Chromium process for a session."""
    info = _sessions.pop(req.session_id, None)
    if info is None:
        # Still clean up UC driver even if patchright session wasn't registered
        _drv = _uc_drivers.pop(req.session_id, None)
        if _drv:
            try:
                _drv.quit()
            except Exception:
                pass
        return {"ok": True, "note": "session not found (already terminated)"}

    guard = info.get("guard_task")
    if guard and not guard.done():
        guard.cancel()

    try:
        if info.get("context"):
            await info["context"].close()
        elif info.get("browser"):
            await info["browser"].close()
    except Exception:
        pass
    try:
        await info["pw"].stop()
    except Exception:
        pass

    # Also quit the UC selenium driver if the Chrome came from UC login
    _drv = _uc_drivers.pop(req.session_id, None)
    if _drv:
        try:
            _drv.quit()
        except Exception:
            pass

    logger.info(f"terminated session={req.session_id}")
    return {"ok": True}


async def inject_cookies(req: InjectCookiesRequest) -> dict:
    """
    Inject UC-harvested Google cookies into the live patchright BrowserContext,
    then navigate to verify_url and confirm authentication.

    Returns:
        {ok, verified, account_email, final_url, cookie_count}
    """
    info = _sessions.get(req.session_id)
    if not info:
        return {"ok": False, "error": f"session {req.session_id} not found"}

    ctx = info.get("context")
    if ctx is None:
        return {"ok": False, "error": "session has no patchright context"}

    try:
        # ── Normalise cookies for playwright format ──────────────────────────
        pw_cookies = []
        for c in req.cookies:
            # Playwright requires 'name', 'value', 'domain', 'path' at minimum.
            # sameSite must be 'Strict'|'Lax'|'None' — map selenium values.
            same_site_map = {
                "strict": "Strict",
                "lax": "Lax",
                "none": "None",
                "no_restriction": "None",
                "unspecified": "Lax",
            }
            ss_raw = str(c.get("sameSite") or c.get("same_site") or "lax").lower()
            ss = same_site_map.get(ss_raw, "Lax")

            entry: dict = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".google.com"),
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure", False)),
                "httpOnly": bool(c.get("httpOnly", c.get("http_only", False))),
                "sameSite": ss,
            }
            if c.get("expiry") or c.get("expires"):
                entry["expires"] = int(c.get("expiry") or c.get("expires"))
            pw_cookies.append(entry)

        await ctx.add_cookies(pw_cookies)
        logger.info(f"inject-cookies: injected {len(pw_cookies)} cookies session={req.session_id}")

        # ── Navigate and verify ──────────────────────────────────────────────
        page = await ctx.new_page()
        try:
            await page.goto(req.verify_url, wait_until="domcontentloaded", timeout=30_000)
            final_url = page.url

            # Detect signed-in state: myaccount shows account info in title/h1
            verified = False
            account_email = None

            # Check URL — signed-out redirects to accounts.google.com/signin
            if "accounts.google.com/signin" in final_url or "ServiceLogin" in final_url:
                verified = False
            else:
                verified = True
                # Try to extract the email from page content
                try:
                    # myaccount.google.com shows email in <title> or aria-label
                    title = await page.title()
                    # Also check a common element
                    el = await page.query_selector('[data-email], [aria-label*="@"]')
                    if el:
                        account_email = await el.get_attribute(
                            "data-email"
                        ) or await el.get_attribute("aria-label")
                    if not account_email and "@" in title:
                        account_email = title.split("(")[-1].strip(")")
                except Exception:
                    pass

            logger.info(
                f"inject-cookies verify: verified={verified} "
                f"email={account_email} url={final_url[:80]}"
            )
            return {
                "ok": True,
                "verified": verified,
                "account_email": account_email,
                "final_url": final_url,
                "cookie_count": len(pw_cookies),
            }
        finally:
            await page.close()

    except Exception as _exc:
        import traceback as _tb_mod

        _tb = _tb_mod.format_exc()
        logger.error(f"inject-cookies EXCEPTION: {_exc}\n{_tb}")
        return {"ok": False, "error": str(_exc), "traceback": _tb[-600:]}


async def navigate_page(req: NavigateRequest) -> dict:
    """
    Navigate the session's patchright context to a URL and keep it open.
    Use this to show pages in XIOVIEW without closing them.
    """
    info = _sessions.get(req.session_id)
    if not info:
        return {"ok": False, "error": f"session {req.session_id} not found"}
    ctx = info.get("context")
    if not ctx:
        return {"ok": False, "error": "session has no patchright context"}
    try:
        # Use existing first page if available, else open new one
        pages = ctx.pages
        page = pages[0] if pages else await ctx.new_page()
        # Retry up to 3× on transient SOCKS5 failures (PPPoE tunnel drop)
        _nav_last_exc = None
        for _nav_attempt in range(3):
            try:
                await page.goto(req.url, wait_until=req.wait_until, timeout=30_000)
                _nav_last_exc = None
                break
            except Exception as _nav_exc:
                _nav_last_exc = _nav_exc
                _emsg = str(_nav_exc)
                if "SOCKS" in _emsg or "ERR_SOCKS" in _emsg or "ERR_TUNNEL" in _emsg:
                    logger.warning(
                        f"navigate: SOCKS failure attempt {_nav_attempt + 1}/3 — {_emsg[:80]}"
                    )
                    await asyncio.sleep(3)
                    continue
                raise  # non-SOCKS error — propagate immediately
        if _nav_last_exc:
            raise _nav_last_exc
        return {"ok": True, "final_url": page.url, "title": await page.title()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
