"""colab.agent.browser.uc_routes — Undetected-chromedriver (UC) login orchestration endpoints.

Handles /run-uc-login (with PG advisory lock and fallback HITL challenge handling),
/run-uc-login-start, /uc-navigate, and /uc-screenshot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from typing import Any

from agent.config import (
    DRIVE_ROOT,
    INTERNAL_SECRET,
    NODE_NAME,
    XIOSYNC_BASE,
    XIOSYNC_TOKEN,
    _SSH_PROXY_URL,
    _STEALTH_UA_CHROME,
)
from agent.display.cdp_proxy import cdp_expose
from agent.models import (
    CDPExposeRequest,
    HITLNotice,
    HITLState,
    UCLoginRequest,
    UCNavigateRequest,
    UCScreenshotRequest,
)
from agent.network.geo import _my_tailscale_ip, _resolve_ip_timezone
from agent.state import _hitl_store, _sessions, _uc_pending_drivers

logger = logging.getLogger("xiorun_agent.browser.uc_routes")

# XIOSYNC_INTERNAL_SECRET fallback alias
XIOSYNC_INTERNAL_SECRET = INTERNAL_SECRET


def _get_run_uc_login_sync():
    """Lazily import _run_uc_login_sync from xiorun_agent if not in local module."""
    try:
        from xiorun_agent import _run_uc_login_sync  # noqa: PLC0415
        return _run_uc_login_sync
    except ImportError:
        import xiorun_agent  # noqa: PLC0415
        return xiorun_agent._run_uc_login_sync


def _launch_uc_chrome_for_login(profile_dir: str, proxy_url: str | None) -> tuple:
    """
    Launch UC Chrome (Chrome 131) for login — returns (driver, port).
    Module-level so /run-uc-login can call it directly (no subprocess).
    Uses use_subprocess=False — same config as the working lifespan Chrome.
    All traffic routes through proxy_url; Colab IP is never exposed.
    """
    import socket as _sock

    import undetected_chromedriver as uc

    # Prefer Chrome 131 (best UC 3.5.5 compatibility)
    _chrome_bin, _chrome_ver = "/opt/chrome131/chrome", 131
    if not (os.path.isfile(_chrome_bin) and os.access(_chrome_bin, os.X_OK)):
        for _p in ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"]:
            if os.path.isfile(_p) and os.access(_p, os.X_OK):
                try:
                    import subprocess as _sp2

                    _raw = _sp2.check_output(
                        [_p, "--version"], timeout=5, stderr=_sp2.DEVNULL
                    ).decode()
                    _chrome_ver = int(_raw.split()[-1].split(".")[0])
                    _chrome_bin = _p
                    break
                except Exception:
                    pass

    opts = uc.ChromeOptions()
    if proxy_url:
        _addr = proxy_url.replace("socks5://", "").replace("socks5h://", "")
        opts.add_argument(f"--proxy-server=socks5://{_addr}")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--use-gl=angle")
    opts.add_argument("--use-angle=gl")
    opts.add_argument("--enable-webgl")
    opts.add_argument("--enable-webgl2")
    opts.add_argument("--ignore-gpu-blocklist")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--remote-allow-origins=*")
    opts.add_experimental_option(
        "prefs",
        {
            "webrtc.ip_handling_policy": "disable_non_proxied_udp",
            "webrtc.multiple_routes_enabled": False,
            "webrtc.nonproxied_udp_enabled": False,
        },
    )

    _s = _sock.socket()
    _s.bind(("", 0))
    port = _s.getsockname()[1]
    _s.close()

    # Set up chromedriver binary
    _our_cd = f"/usr/local/bin/chromedriver{_chrome_ver}"
    if not os.path.isfile(_our_cd):
        _our_cd = "/usr/local/bin/chromedriver131"
    _uc_cd_dir = os.path.expanduser("~/.local/share/undetected_chromedriver")
    _uc_cd_path = os.path.join(_uc_cd_dir, "undetected_chromedriver")
    if os.path.isfile(_our_cd):
        os.makedirs(_uc_cd_dir, exist_ok=True)
        import shutil as _sh2

        # Copy to a temp file first, then atomically rename.
        # Direct copy2 to the destination while another Chrome is running causes
        # ETXTBSY (Text file busy) — Linux won't let you overwrite an executing binary.
        # An atomic rename replaces the directory entry without touching the open inode,
        # so the running Chrome keeps its old fd while the new Chrome gets the fresh copy.
        _uc_cd_tmp = _uc_cd_path + f".tmp.{os.getpid()}"
        try:
            _sh2.copy2(_our_cd, _uc_cd_tmp)
            os.chmod(_uc_cd_tmp, 0o755)
            os.replace(_uc_cd_tmp, _uc_cd_path)  # atomic on Linux (POSIX rename)
        except OSError as _cd_err:
            # If rename fails (e.g. cross-device), clean up and proceed without copy.
            # undetected_chromedriver will patch its own copy on first run.
            try:
                os.unlink(_uc_cd_tmp)
            except OSError:
                pass
            logger.warning(
                f"_launch_uc_chrome_for_login: chromedriver copy failed ({_cd_err}) — using existing"
            )

    # Clean stale profile locks
    os.makedirs(profile_dir, exist_ok=True)
    for _lk in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try:
            os.remove(os.path.join(profile_dir, _lk))
        except FileNotFoundError:
            pass

    logger.info(
        f"_launch_uc_chrome_for_login: Chrome {_chrome_ver} proxy={bool(proxy_url)} port={port}"
    )
    driver = uc.Chrome(
        options=opts,
        browser_executable_path=_chrome_bin,
        driver_executable_path=_uc_cd_path if os.path.isfile(_uc_cd_path) else None,
        version_main=_chrome_ver,
        use_subprocess=False,
        headless=False,
        port=port,
        user_data_dir=profile_dir,
        keep_user_data_dir=True,
    )
    return driver, port


async def run_uc_login(req: UCLoginRequest) -> dict:
    """
    Run UC stealth login using Google Chrome 153.

    Picks up exit node proxy + stealth UA from the session (_sessions dict).
    Resolves timezone from exit_node_public_ip (or session cached timezone).
    Returns Google cookies to inject into a patchright browser context.
    """
    session_info = _sessions.get(req.session_id or "", {})
    proxy_url = req.proxy_url or session_info.get("proxy_url")
    user_agent = _STEALTH_UA_CHROME

    # Resolve timezone: prefer request field → session cache → runtime auto-detect
    _exit_ip = req.exit_node_public_ip or session_info.get("exit_node_ip")
    if session_info.get("timezone") and not req.exit_node_public_ip:
        # Session already resolved timezone at launch — reuse it (saves ip-api call)
        _tz = session_info["timezone"]
    else:
        _tz = await asyncio.get_event_loop().run_in_executor(None, _resolve_ip_timezone, _exit_ip)

    logger.info(
        f"run-uc-login session={req.session_id} "
        f"proxy={'yes: ' + proxy_url[:40] if proxy_url else 'no (DIRECT — Colab IP)'} "
        f"timezone={_tz}"
    )

    # ── HARD BLOCK: Colab datacenter IP is NOT allowed ───────────────────────────
    # All browser sessions MUST route through a PPPoE exit node (Mac-hosted VM TS IP).
    # Reject immediately if no proxy is supplied — do NOT fall back to Colab IP.
    if not proxy_url:
        logger.error(
            "run-uc-login BLOCKED: no exit node proxy provided. "
            "Colab datacenter IP is forbidden for Google sign-in. "
            "Acquire a PPPoE slot from XIOSYNC and pass its proxy_url."
        )
        return {
            "ok": False,
            "error": (
                "EXIT_NODE_REQUIRED: No proxy_url provided. "
                "Colab datacenter IPs are blocked. "
                "Acquire an idle PPPoE exit-node slot from XIOSYNC "
                "(POST /api/v1/pppoe/nodes/acquire) and pass its proxy_url."
            ),
        }

    # Named UC profile dir — survives driver.quit() so patchright can reuse it.
    # Deterministic path convention (matches profile_store.py):
    #   /tmp/xiorun_profiles/PRFL_{id16}__{node_slug}/
    # Falls back to email-slug when identity_id not available.
    import re as _re_prof

    _profile_dir = None
    _identity_id_str = req.identity_id if hasattr(req, "identity_id") and req.identity_id else ""
    _node_slug = _re_prof.sub(r"[^a-zA-Z0-9-]", "-", NODE_NAME or "default")
    if _identity_id_str:
        _id_short = _identity_id_str.replace("-", "")[:16]
        import glob as _prfl_g

        # Check for pre-pulled PRFL profile (from ChromeProfileStore.restore)
        _pulled = sorted(_prfl_g.glob(f"/tmp/xiorun_profiles/PRFL_{_id_short}*"))
        if _pulled:
            _profile_dir = str(_pulled[-1])
    if not _profile_dir:
        # Create deterministic dir — stable across restarts for the same identity
        if _identity_id_str:
            _id_short = _identity_id_str.replace("-", "")[:16]
            _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_id_short}__{_node_slug}"
        else:
            # Last resort: derive from email
            _email_slug = _re_prof.sub(r"[^a-zA-Z0-9]", "_", req.email.split("@")[0].lower())[:32]
            _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_email_slug}__{_node_slug}"
        os.makedirs(_profile_dir, exist_ok=True)
    logger.info(f"run-uc-login: profile_dir={_profile_dir}")

    # ── Domain-aware eviction before login (XIOBR port) ──────────────────────
    # Strip only target domain (google.com) cookies from reused profiles.
    # All other domain cookies are preserved dynamically — no hardcoded list.
    if os.path.isdir(_profile_dir) and any(os.scandir(_profile_dir)):
        try:
            from xiosync.subsystems.xiorun.domain_eviction import evict_local_profile_cookies

            _evicted = evict_local_profile_cookies(_profile_dir, "google.com")
            if _evicted > 0:
                logger.info(f"run-uc-login: evicted {_evicted} non-google.com cookies from profile")
        except ImportError:
            # Module not available on worker — try inline SQLite eviction
            try:
                import sqlite3 as _sq_ev

                _cookies_db = os.path.join(_profile_dir, "Default", "Cookies")
                if os.path.exists(_cookies_db):
                    _conn = _sq_ev.connect(_cookies_db, timeout=5.0)
                    _cur = _conn.cursor()
                    _cur.execute("DELETE FROM cookies WHERE host_key NOT LIKE ?", ("%google.com%",))
                    _evicted = _cur.rowcount
                    _conn.commit()
                    _conn.close()
                    if _evicted > 0:
                        logger.info(
                            f"run-uc-login: evicted {_evicted} non-google.com cookies (inline)"
                        )
            except Exception as _ev_err:
                logger.warning(f"run-uc-login: inline eviction failed: {_ev_err}")
        except Exception as _ev_exc:
            logger.warning(f"run-uc-login: domain eviction error: {_ev_exc}")
    # ── Clean detection-relevant profile artifacts ─────────────────────────────
    # Remove localStorage, IndexedDB, BrowsingTopics, Cache and Code Cache from
    # profile before login — they accumulate cross-session fingerprint signals
    # that Google uses to correlate device identity across login attempts.
    # Cookies are handled separately via domain eviction above.
    _default_dir = os.path.join(_profile_dir, "Default")
    _artifacts_to_clean = [
        "Cache",
        "Code Cache",
        "GPUCache",
        "Local Storage",
        "IndexedDB",
        "BrowsingTopicsSiteData",
        "BrowsingTopicsState",
        "Session Storage",
    ]
    if os.path.isdir(_default_dir):
        for _art in _artifacts_to_clean:
            _art_path = os.path.join(_default_dir, _art)
            try:
                if os.path.isdir(_art_path):
                    import shutil as _shu

                    _shu.rmtree(_art_path, ignore_errors=True)
                elif os.path.isfile(_art_path):
                    os.remove(_art_path)
            except Exception as _art_err:
                logger.debug(f"run-uc-login: artifact clean skipped {_art}: {_art_err}")
        logger.info("run-uc-login: pre-login profile artifact cleanup done")

    loop = asyncio.get_event_loop()

    # ── Per-identity distributed login lock (PG advisory via XIOSYNC API) ────
    # Prevents two workers from signing into the same account concurrently.
    _lock_resource = f"google-signin/{req.identity_id or req.email}"
    _lock_acquired = False
    if XIOSYNC_BASE:
        try:
            import json as _json_lk
            import urllib.request

            _lk_url = f"{XIOSYNC_BASE}/api/v1/workers/lock/pg/acquire"
            _lk_data = _json_lk.dumps(
                {
                    "resource_key": _lock_resource,
                    "node_name": NODE_NAME,
                    "ttl_seconds": 180,
                }
            ).encode()
            _lk_req = urllib.request.Request(
                _lk_url,
                data=_lk_data,
                headers={"Content-Type": "application/json", "X-Worker-Secret": XIOSYNC_TOKEN},
                method="POST",
            )
            with urllib.request.urlopen(_lk_req, timeout=5) as _lk_resp:
                _lk_result = _json_lk.loads(_lk_resp.read())
                _lock_acquired = _lk_result.get("acquired", False)
                if not _lock_acquired:
                    _holder = _lk_result.get("holder", "unknown")
                    logger.warning(
                        f"run-uc-login: login lock held by {_holder} — proceeding anyway (non-blocking)"
                    )
                else:
                    logger.info(f"run-uc-login: login lock acquired for {_lock_resource}")
        except Exception as _lk_err:
            logger.warning(
                f"run-uc-login: lock acquire failed ({_lk_err}) — proceeding without lock"
            )

    try:
        # ── IN-PROCESS APPROACH (replaces subprocess runner) ──────────────────────
        loop = asyncio.get_event_loop()
        # ── CRITICAL: use a FRESH per-email profile for the login Chrome ──────────
        _email_slug = re.sub(r"[^a-zA-Z0-9]", "_", req.email.split("@")[0])[:24]
        _login_profile_dir = f"/tmp/uc_login_{_email_slug}"
        logger.info(f"uc-login: using fresh login profile: {_login_profile_dir}")

        logger.info("uc-login: launching Chrome in-process via _launch_uc_chrome_for_login()...")
        _uc_drv, _uc_port = await loop.run_in_executor(
            None, lambda: _launch_uc_chrome_for_login(_login_profile_dir, proxy_url)
        )
        logger.info(f"uc-login: Chrome ready on port {_uc_port}, running login in executor...")

        _run_sync_fn = _get_run_uc_login_sync()
        result = await loop.run_in_executor(
            None,
            lambda: _run_sync_fn(
                email=req.email,
                password=req.password,
                totp_secret=req.totp_secret or "",
                proxy_url=proxy_url,
                user_agent=user_agent,
                user_data_dir=_login_profile_dir,  # fresh profile — not cascade PRFL
                pre_launched_driver=_uc_drv,
                exit_node_public_ip=req.exit_node_public_ip or None,
                fingerprint=req.fingerprint if hasattr(req, "fingerprint") else None,
            ),
        )
        logger.info(
            f"uc-login: in-process result: ok={result.get('ok')} url={result.get('url', '?')}"
        )
        # Attach port so cookie injection downstream knows where Chrome lives
        if "uc_port" not in result:
            result["uc_port"] = _uc_port

        if result.get("ok") and result.get("uc_port"):
            # ── Register session so /health and other endpoints can reference it ──
            _uc_port = result["uc_port"]
            _tailscale_ip = _my_tailscale_ip()
            _sessions[req.session_id] = {
                "browser": None,  # UC Chrome — Selenium manages it, not patchright
                "context": None,
                "pw": None,
                "pid": 0,
                "port": _uc_port,
                "cdp_ws_url": f"ws://{_tailscale_ip}:{_uc_port}",  # placeholder
                "proxy_url": proxy_url,
                "profile_dir": _profile_dir,
                "chrome_proc": None,
                "guard_task": None,
                "timezone": _tz,
                "exit_node_ip": _exit_ip,
            }

            # ── Auto-expose: make Chrome CDP accessible on Tailscale via TCP forwarder ──
            _uc_session_id = req.session_id or str(uuid.uuid4())
            # Register session with auto-generated id before expose
            if _uc_session_id not in _sessions:
                _sessions[_uc_session_id] = {
                    "context": None,
                    "proxy_url": proxy_url,
                    "cdp_ws_url": f"ws://{_tailscale_ip}:{_uc_port}",
                }
            try:
                _expose_req = CDPExposeRequest(session_id=_uc_session_id, local_port=_uc_port)
                _expose_result = await cdp_expose(_expose_req)
                _cdp_ws = _expose_result["cdp_ws_url"]
                _sessions[_uc_session_id]["cdp_ws_url"] = _cdp_ws
                logger.info(
                    f"run-uc-login: CDP exposed session={_uc_session_id} "
                    f"tailscale={_expose_result['cdp_http_url']}"
                )
            except Exception as _expose_exc:
                _cdp_ws = f"ws://{_tailscale_ip}:{_uc_port}"
                logger.warning(f"run-uc-login: cdp-expose failed: {_expose_exc} — using direct URL")

            result["cdp_ws_url"] = _cdp_ws
            result["cdp_http_url"] = _cdp_ws.replace("ws://", "http://")
            result["session_id"] = _uc_session_id  # fix: mjs reads ucResult.session_id
            result["profile_dir"] = _login_profile_dir  # fix: mjs reads ucResult.profile_dir
            logger.info(
                f"run-uc-login: session registered session={req.session_id} port={_uc_port} "
                f"cdp_ws={_cdp_ws}"
            )

            # Write enriched fingerprint JSON for this profile
            try:
                import datetime as _dt
                import json as _json

                _prfl_id = (
                    req.identity_id.replace("-", "")[:16]
                    if (hasattr(req, "identity_id") and req.identity_id)
                    else req.email.split("@")[0]
                )
                _prfl_serial_fp = getattr(req, "profile_serial", 0) or 0

                # ── Derive hardware/display attributes from the UA string ──────
                _ua_fp = result.get("ua_string", "")
                _tz_fp = result.get("timezone", "UTC")
                _lo_fp = result.get("locale", "en-US")

                # Derive platform from UA (Windows/Mac/Linux/Android/iOS)
                if "Windows" in _ua_fp:
                    _platform = "Win32"
                    _screen_w, _screen_h = 1920, 1080
                    _color_depth = 24
                    _hw_concurrency = 8
                    _device_memory = 8
                    _max_touch = 0
                elif "Macintosh" in _ua_fp:
                    _platform = "MacIntel"
                    _screen_w, _screen_h = 2560, 1440
                    _color_depth = 30
                    _hw_concurrency = 10
                    _device_memory = 16
                    _max_touch = 0
                elif "Linux" in _ua_fp:
                    _platform = "Linux x86_64"
                    _screen_w, _screen_h = 1920, 1080
                    _color_depth = 24
                    _hw_concurrency = 4
                    _device_memory = 4
                    _max_touch = 0
                else:
                    _platform = "Win32"
                    _screen_w, _screen_h = 1920, 1080
                    _color_depth = 24
                    _hw_concurrency = 8
                    _device_memory = 8
                    _max_touch = 0

                # WebGL renderer/vendor — seeded from canvas_seed for consistency
                _c_seed = result.get("canvas_seed", 0)
                _webgl_renderers = [
                    ("ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)", "Google Inc. (NVIDIA)"),
                    ("ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)", "Google Inc. (Intel)"),
                    ("ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)", "Google Inc. (AMD)"),
                    ("ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)", "Google Inc. (NVIDIA)"),
                ]
                _webgl_renderer, _webgl_vendor = _webgl_renderers[_c_seed % len(_webgl_renderers)]

                _fp_data = {
                    # ── Identity ──────────────────────────────────────────────
                    "profile_id": _prfl_id,
                    "profile_serial": _prfl_serial_fp,
                    "email": req.email,
                    "identity_id": req.identity_id if hasattr(req, "identity_id") else "",
                    # ── Noise seeds (deterministic per-profile) ────────────────
                    "canvas_seed": result.get("canvas_seed", 0),
                    "audio_seed": result.get("audio_seed", 0),
                    "webgl_seed": result.get("webgl_seed", 0),
                    # ── Navigator / UA ─────────────────────────────────────────
                    "ua": _ua_fp,
                    "platform": _platform,
                    "vendor": "Google Inc.",
                    "language": _lo_fp,
                    "languages": [_lo_fp, _lo_fp.split("-")[0]],
                    "hardware_concurrency": _hw_concurrency,
                    "device_memory": _device_memory,
                    "max_touch_points": _max_touch,
                    # ── Screen / Display ───────────────────────────────────────
                    "screen_width": _screen_w,
                    "screen_height": _screen_h,
                    "color_depth": _color_depth,
                    "pixel_ratio": 1.0 if _color_depth == 24 else 2.0,
                    # ── WebGL ──────────────────────────────────────────────────
                    "webgl_renderer": _webgl_renderer,
                    "webgl_vendor": _webgl_vendor,
                    # ── Locale / Time ──────────────────────────────────────────
                    "timezone": _tz_fp,
                    "locale": _lo_fp,
                    # ── Meta ───────────────────────────────────────────────────
                    "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
                    "updated_at": _dt.datetime.now(_dt.UTC).isoformat(),
                    "schema_version": 2,
                }

                # ── Save to Drive (fast local cache, named by PRFL-NNN for scan) ──
                _prfl_label = f"PRFL-{_prfl_serial_fp:03d}" if _prfl_serial_fp else f"PRFL_{_prfl_id}"
                _fp_path_serial = os.path.join(
                    DRIVE_ROOT, "profiles", f"{_prfl_label}.fingerprint.json"
                )
                _fp_path_hex = os.path.join(
                    DRIVE_ROOT, "profiles", f"PRFL_{_prfl_id}.fingerprint.json"
                )
                os.makedirs(os.path.dirname(_fp_path_serial), exist_ok=True)
                with open(_fp_path_serial, "w") as _ff:
                    _json.dump(_fp_data, _ff, indent=2)
                # Symlink hex-id path → serial path for backward compat
                if _fp_path_hex != _fp_path_serial and not os.path.exists(_fp_path_hex):
                    try:
                        os.symlink(_fp_path_serial, _fp_path_hex)
                    except Exception:
                        with open(_fp_path_hex, "w") as _ff2:
                            _json.dump(_fp_data, _ff2, indent=2)
                logger.info(f"run-uc-login: fingerprint saved → {_fp_path_serial} (schema_v2, {len(_fp_data)} fields)")

                # ── Vault the fingerprint (encrypted, survives Drive wipe) ────
                _vault_iid = req.identity_id if (hasattr(req, "identity_id") and req.identity_id) else ""
                if _vault_iid and XIOSYNC_BASE:
                    try:
                        import httpx as _hx_fp  # noqa: PLC0415

                        _fp_vault_key = f"identities/{_vault_iid}/fingerprint"
                        _fp_r = _hx_fp.post(
                            f"{XIOSYNC_BASE}/api/v1/xioflow/events/vault-fingerprint-internal",
                            headers={"X-XIOSYNC-Internal": XIOSYNC_INTERNAL_SECRET,
                                     "Content-Type": "application/json"},
                            json={"fingerprint": _fp_data, "identity_id": _vault_iid,
                                  "org_id": "00000000-0000-7000-8000-000000000000"},
                            timeout=8,
                        )
                        if _fp_r.status_code == 200:
                            logger.info(f"run-uc-login: fingerprint vaulted → {_fp_vault_key}")
                        else:
                            logger.warning(f"run-uc-login: fingerprint vault HTTP {_fp_r.status_code}")
                    except Exception as _fve:
                        logger.warning(f"run-uc-login: fingerprint vault skipped ({_fve})")

            except Exception as _fe:
                logger.warning(f"run-uc-login: fingerprint save failed: {_fe}")

        # ── HITL pause on recoverable failure (XIOBR port) ───────────────────
        if not result.get("ok"):
            _error_msg = result.get("error", "")
            _recoverable = True  # any ok=False from run-uc-login gets HITL
            if _recoverable:
                try:
                    import uuid as _hitl_uuid

                    _org_id = _hitl_uuid.UUID("00000000-0000-7000-8000-000000000000")
                    _xioview_url = (
                        f"{XIOSYNC_BASE}/api/v1/xioview/sessions/prfl002-login/view"
                        if XIOSYNC_BASE
                        else "XIOVIEW: prfl002-login"
                    )
                    _notice = HITLNotice(
                        organization_id=_org_id,
                        session_id="prfl002-login",
                        challenge_type="recaptcha"
                        if "captcha" in _error_msg.lower() or "rejected" in _error_msg.lower()
                        else "login_failed",
                        message=(
                            f"Google sign-in challenge for {req.email}.\n"
                            f"Error: {_error_msg[:200]}\n"
                            f"👁 Watch live: {_xioview_url}\n"
                            f"Action: Solve reCAPTCHA or verify account manually in the XIOVIEW browser, then Resume."
                        ),
                        identity_id=_hitl_uuid.UUID(req.identity_id) if req.identity_id else None,
                        instructions=(
                            "Open XIOVIEW URL above. Solve the reCAPTCHA or complete the verification. "
                            'Then POST /hitl/{id}/resume with {"action": "resume"} to continue login.'
                        ),
                    )
                    _hitl_store.create(_notice)
                    _notice_id = str(_notice.id)
                    logger.info(
                        f"run-uc-login: ✅ HITL notice created id={_notice_id} — "
                        f"view={_xioview_url} — waiting up to 300s for operator resume"
                    )
                    result["hitl_notice_id"] = _notice_id
                    result["hitl_state"] = "PENDING"
                    result["xioview_url"] = _xioview_url
                    result["hitl_resume_url"] = (
                        f"http://127.0.0.1:{os.environ.get('XIORUN_AGENT_PORT', '9300')}/hitl/{_notice_id}/resume"
                    )

                    # ── Block and wait for operator to resume (up to 300s) ────
                    import asyncio as _hitl_asyncio

                    _hitl_event = _hitl_store._events.get(_notice.id)
                    if _hitl_event:
                        logger.info("run-uc-login: pausing — waiting for HITL resume...")
                        try:
                            await _hitl_asyncio.wait_for(_hitl_event.wait(), timeout=300.0)
                        except TimeoutError:
                            pass

                        _resumed_notice = _hitl_store._notices.get(_notice.id)
                        if _resumed_notice and _resumed_notice.state == HITLState.RESUMED:
                            logger.info(
                                "run-uc-login: HITL resumed by operator — checking state via CDP"
                            )
                            try:
                                import json as _jr_cdp
                                import time as _tc_cdp
                                import urllib.request as _ur_cdp

                                import websocket as _ws_cdp

                                _cdp_port = _uc_port or result.get("uc_port", 0)
                                _post_hitl_url = ""
                                _hitl_cookies = []
                                if _cdp_port:
                                    _tabs = _jr_cdp.loads(
                                        _ur_cdp.urlopen(
                                            f"http://127.0.0.1:{_cdp_port}/json", timeout=4
                                        ).read()
                                    )
                                    _pg = next((t for t in _tabs if t.get("type") == "page"), None)
                                    if _pg:
                                        _post_hitl_url = _pg.get("url", "")
                                        logger.info(
                                            f"run-uc-login: post-HITL CDP URL: {_post_hitl_url[:100]}"
                                        )
                                        # Extract cookies if on authenticated page
                                        if (
                                            "mail.google.com" in _post_hitl_url
                                            or "myaccount.google.com" in _post_hitl_url
                                        ):
                                            _ws2 = _ws_cdp.create_connection(
                                                _pg["webSocketDebuggerUrl"], timeout=8
                                            )
                                            _ws2.send(
                                                _jr_cdp.dumps(
                                                    {
                                                        "id": 1,
                                                        "method": "Network.getAllCookies",
                                                        "params": {},
                                                    }
                                                )
                                            )
                                            _tc_cdp.sleep(1.5)
                                            _ws2.settimeout(8)
                                            _cr = _jr_cdp.loads(_ws2.recv())
                                            _hitl_cookies = _cr.get("result", {}).get("cookies", [])
                                            _ws2.close()
                                            result["ok"] = True
                                            result["error"] = ""
                                            result["cookies"] = _hitl_cookies
                                            result["final_url"] = _post_hitl_url
                                            result["hitl_state"] = "RESUMED_SUCCESS"
                                            logger.info(
                                                f"run-uc-login: ✅ HITL login verified — "
                                                f"{len(_hitl_cookies)} cookies extracted"
                                            )
                                        elif (
                                            "accounts.google.com" not in _post_hitl_url
                                            and _post_hitl_url
                                        ):
                                            result["ok"] = True
                                            result["hitl_state"] = "RESUMED_SUCCESS"
                                            logger.info(
                                                f"run-uc-login: ✅ Navigated away from Google login: {_post_hitl_url[:80]}"
                                            )
                                        else:
                                            logger.warning(
                                                f"run-uc-login: still on login page after HITL: {_post_hitl_url[:80]}"
                                            )
                                else:
                                    logger.warning(
                                        "run-uc-login: no CDP port — cannot verify post-HITL state"
                                    )
                            except Exception as _phr:
                                logger.warning(f"run-uc-login: post-HITL CDP check error: {_phr}")
                        else:
                            logger.warning(
                                "run-uc-login: HITL timed out (300s) — no operator action"
                            )
                            result["hitl_state"] = "EXPIRED"

                except Exception as _hitl_err:
                    logger.warning(f"run-uc-login: HITL creation failed: {_hitl_err}")

        return result
    except Exception as _exc:
        import traceback

        _tb = traceback.format_exc()
        logger.error(f"run-uc-login EXCEPTION: {_exc}\n{_tb}")
        return {"ok": False, "error": str(_exc), "traceback": _tb[-800:]}
    finally:
        # ── Release per-identity login lock ──────────────────────────────────
        if _lock_acquired and XIOSYNC_BASE:
            try:
                import json as _json_lkr
                import urllib.request

                _lkr_url = f"{XIOSYNC_BASE}/api/v1/workers/lock/pg/release"
                _lkr_data = _json_lkr.dumps(
                    {
                        "resource_key": _lock_resource,
                        "node_name": NODE_NAME,
                    }
                ).encode()
                _lkr_req = urllib.request.Request(
                    _lkr_url,
                    data=_lkr_data,
                    headers={"Content-Type": "application/json", "X-Worker-Secret": XIOSYNC_TOKEN},
                    method="POST",
                )
                with urllib.request.urlopen(_lkr_req, timeout=5) as _lkr_resp:
                    logger.info(f"run-uc-login: login lock released for {_lock_resource}")
            except Exception as _lkr_err:
                logger.warning(f"run-uc-login: lock release failed ({_lkr_err})")


async def run_uc_login_start(req: UCLoginRequest) -> dict:
    """
    Step 1 of 2-step UC login.
    Launches UC Chrome and returns its CDP port IMMEDIATELY — before any login occurs.
    Caller should:
      1. Attach XIOVIEW to the returned uc_port → user watches Chrome open
      2. Call POST /run-uc-login with the same session_id → login runs in the visible browser

    The pre-launched driver is stored in _uc_pending_drivers[session_id] for /run-uc-login to reuse.
    """

    session_info = _sessions.get(req.session_id or "", {})
    proxy_url = req.proxy_url or session_info.get("proxy_url") or _SSH_PROXY_URL

    if not proxy_url:
        return {
            "ok": False,
            "error": "EXIT_NODE_REQUIRED: no proxy_url provided and no bridge running",
        }

    # Deterministic path convention
    _node_slug = NODE_NAME.replace("-", "_")
    _re_prof = re.compile(r"[^a-zA-Z0-9]")
    if req.identity_id:
        _id16 = req.identity_id.replace("-", "")[:16]
        _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_id16}__{_node_slug}"
    else:
        _email_slug = _re_prof.sub("_", req.email.split("@")[0].lower())[:32]
        _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_email_slug}__{_node_slug}"

    os.makedirs(_profile_dir, exist_ok=True)
    logger.info(f"run-uc-login-start: using profile_dir={_profile_dir}")
    # Clean up stale Chrome lock files that would prevent reusing saved profile
    for _lock in ["SingletonLock", "SingletonSocket", "Default/LOCK"]:
        _lpath = os.path.join(_profile_dir, _lock)
        try:
            os.remove(_lpath)
        except FileNotFoundError:
            pass

    def _launch_uc_chrome():
        """Just launch UC Chrome and return (driver, uc_port) — no login."""
        import socket as _sock
        import subprocess

        import undetected_chromedriver as uc

        def _find_chrome():
            import glob as _g

            # 1. Chrome 131 — optimal for UC 3.5.5 (fully patches webdriver artifacts)
            if os.path.isfile("/opt/chrome131/chrome") and os.access(
                "/opt/chrome131/chrome", os.X_OK
            ):
                return "/opt/chrome131/chrome", 131
            # 2. patchright Chromium — prefer ≤133, accept 153 as last resort
            _pr_best = None
            for path in sorted(
                _g.glob("/root/.cache/ms-patchright/chromium-*/chrome-linux64/chrome"), reverse=True
            ):
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    try:
                        raw = (
                            subprocess.check_output(
                                [path, "--version"], timeout=5, stderr=subprocess.DEVNULL
                            )
                            .decode()
                            .strip()
                        )
                        major = int(raw.split()[-1].split(".")[0])
                        if major <= 133:
                            return path, major
                        elif _pr_best is None or major < _pr_best[1]:
                            _pr_best = (path, major)
                    except Exception:
                        pass
            if _pr_best:
                logger.warning(
                    f"uc-login-start: using patchright Chromium {_pr_best[1]} (>133, detection risk higher)"
                )
                return _pr_best
            # 3. System Chrome ≤133 only
            for path in ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"]:
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    try:
                        raw = (
                            subprocess.check_output(
                                [path, "--version"], timeout=5, stderr=subprocess.DEVNULL
                            )
                            .decode()
                            .strip()
                        )
                        major = int(raw.split()[-1].split(".")[0])
                        if major <= 133:
                            return path, major
                    except Exception:
                        pass
            raise FileNotFoundError("No Chrome binary found. Install Chrome 131 or patchright.")

        chrome_bin, chrome_ver = _find_chrome()

        opts = uc.ChromeOptions()
        proxy_addr = proxy_url.replace("socks5://", "")
        opts.add_argument(f"--proxy-server=socks5://{proxy_addr}")

        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--use-gl=angle")
        opts.add_argument("--use-angle=gl")  # EGL/Mesa, not swiftshader
        opts.add_argument("--enable-webgl")
        opts.add_argument("--enable-webgl2")
        opts.add_argument("--ignore-gpu-blocklist")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-service-workers")
        opts.add_argument("--disable-features=ServiceWorker")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--window-position=0,0")
        opts.add_argument("--display=:99")
        # Prevent Chrome from restoring previous session when launched with a saved profile
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--restore-last-session=false")
        opts.add_argument("--disable-session-crashed-bubble")
        opts.add_argument("--disable-infobars")
        opts.add_experimental_option(
            "prefs",
            {
                "webrtc.ip_handling_policy": "disable_non_proxied_udp",
                "webrtc.multiple_routes_enabled": False,
                "webrtc.nonproxied_udp_enabled": False,
            },
        )

        _s = _sock.socket()
        _s.bind(("", 0))
        port = _s.getsockname()[1]
        _s.close()

        # Allow XIOVIEW server (Mac Mini) to connect CDP WS without origin rejection
        opts.add_argument("--remote-allow-origins=*")

        _our_cd = "/usr/local/bin/chromedriver131"
        _uc_cd_dir = os.path.expanduser("~/.local/share/undetected_chromedriver")
        _uc_cd_path = os.path.join(_uc_cd_dir, "undetected_chromedriver")
        if os.path.isfile(_our_cd):
            os.makedirs(_uc_cd_dir, exist_ok=True)
            import shutil as _sh

            _uc_cd_tmp = _uc_cd_path + f".tmp.{os.getpid()}"
            try:
                _sh.copy2(_our_cd, _uc_cd_tmp)
                os.chmod(_uc_cd_tmp, 0o755)
                os.replace(_uc_cd_tmp, _uc_cd_path)
            except OSError as _cd_err2:
                try:
                    os.unlink(_uc_cd_tmp)
                except OSError:
                    pass
                logger.warning(
                    f"_launch_uc_chrome: chromedriver copy failed ({_cd_err2}) — using existing"
                )
        _drv_path = _uc_cd_path if os.path.isfile(_uc_cd_path) else None

        driver = uc.Chrome(
            options=opts,
            browser_executable_path=chrome_bin,
            driver_executable_path=_drv_path,
            version_main=chrome_ver,
            use_subprocess=False,  # True causes 'chrome not reachable' on Colab (port lost)
            headless=False,
            port=port,
            user_data_dir=_profile_dir,
            keep_user_data_dir=True,
        )
        return driver, port

    loop = asyncio.get_event_loop()
    try:
        driver, uc_port = await loop.run_in_executor(None, _launch_uc_chrome)
        _uc_pending_drivers[req.session_id] = driver
        _tailscale_ip = _my_tailscale_ip()
        logger.info(f"run-uc-login-start: UC Chrome ready session={req.session_id} port={uc_port}")
        return {
            "ok": True,
            "uc_port": uc_port,
            "cdp_ws_url": f"ws://{_tailscale_ip}:{uc_port}",
            "profile_dir": _profile_dir,
            "message": "UC Chrome launched. Attach XIOVIEW, then POST /run-uc-login to start login.",
        }
    except Exception as exc:
        logger.error(f"run-uc-login-start EXCEPTION: {exc}")
        return {"ok": False, "error": str(exc)}


async def uc_navigate(req: UCNavigateRequest) -> dict:
    """Navigate the pre-launched UC driver (from /run-uc-login-start) to a URL.
    Also takes a screenshot and returns it base64-encoded.
    """
    sid = req.session_id or ""
    driver = _uc_pending_drivers.get(sid)
    if driver is None and _uc_pending_drivers:
        driver = next(iter(_uc_pending_drivers.values()))
    if driver is None:
        return {"ok": False, "error": "No pending UC driver. Run /run-uc-login-start first."}

    def _do_navigate():
        driver.get(req.url)
        import time as _t

        _t.sleep(req.wait_seconds)
        return driver.current_url, driver.title, driver.get_screenshot_as_base64()

    loop = asyncio.get_event_loop()
    try:
        current_url, title, png_b64 = await loop.run_in_executor(None, _do_navigate)
        logger.info(f"uc-navigate: {req.url!r} → {current_url!r} title={title!r}")
        return {
            "ok": True,
            "current_url": current_url,
            "title": title,
            "screenshot_png_b64": png_b64,
        }
    except Exception as exc:
        logger.error(f"uc-navigate error: {exc}")
        return {"ok": False, "error": str(exc)}


async def uc_screenshot(req: UCScreenshotRequest) -> dict:
    """Take a screenshot of the pre-launched UC driver."""
    sid = req.session_id or ""
    driver = _uc_pending_drivers.get(sid)
    if driver is None and _uc_pending_drivers:
        driver = next(iter(_uc_pending_drivers.values()))
    if driver is None:
        return {"ok": False, "error": "No pending UC driver"}

    def _snap():
        return driver.current_url, driver.title, driver.get_screenshot_as_base64()

    loop = asyncio.get_event_loop()
    try:
        url, title, b64 = await loop.run_in_executor(None, _snap)
        return {"ok": True, "current_url": url, "title": title, "screenshot_png_b64": b64}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
