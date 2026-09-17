"""xiorun_agent.py — Thin FastAPI agent for XIORUN on Colab workers.

Started by boot.py Phase 7 on each Colab runtime.
Listens on port 9300 (Tailscale-accessible only).

Endpoints:
    POST /launch         — start patchright Chromium for a session
    POST /terminate      — kill Chromium for a session
    POST /pull-profile   — fetch Chrome profile tar.gz from R2, extract locally
    POST /push-profile   — tar local Chrome profile, upload to R2
    GET  /health         — liveness check
    GET  /sessions       — list active sessions + CDP URLs

Background per-session: SOCKS5 proxy liveness probe every 10s.
On proxy loss → POST /xiosync/.../proxy-lost (dual enforcement with Mac exit_guard).

Environment variables (set by boot.py):
    XIORUN_AGENT_PORT       — listen port (default 9300)
    XIORUN_R2_ENDPOINT      — Cloudflare R2 endpoint URL
    XIORUN_R2_BUCKET        — R2 bucket name (default: xio-profiles)
    XIORUN_R2_ACCESS_KEY    — R2 access key ID
    XIORUN_R2_SECRET_KEY    — R2 secret access key
    XIORUN_XIOSYNC_BASE     — XIOSYNC API base URL for proxy-lost callback
    XIORUN_XIOSYNC_TOKEN    — Bearer token for XIOSYNC API calls
    XIO_NODE_NAME           — this Colab node's slug (e.g. colab-worker-001)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("xiorun_agent")

app = FastAPI(title="XIORUN Agent", version="1.0.0")

# ── Configuration ──────────────────────────────────────────────────────────────
AGENT_PORT    = int(os.environ.get("XIORUN_AGENT_PORT", "9300"))
DRIVE_ROOT    = os.environ.get("XIO_DRIVE_ROOT", "/content/drive/MyDrive/XIOSYNC-Shared")
XIOSYNC_BASE  = os.environ.get("XIOSYNC_BASE", os.environ.get("XIORUN_XIOSYNC_BASE", ""))
XIOSYNC_TOKEN = os.environ.get("WORKER_SECRET", os.environ.get("XIORUN_XIOSYNC_TOKEN", ""))
NODE_NAME     = os.environ.get("NODE_NAME", os.environ.get("XIO_NODE_NAME", "colab-agent"))

PROFILES_BASE = Path(tempfile.gettempdir()) / "xiorun_profiles"
PROFILES_BASE.mkdir(exist_ok=True)

TRIM_DIRS = [
    "Cache", "Code Cache", "GPUCache", "DawnCache", "ShaderCache",
    os.path.join("Service Worker", "CacheStorage"),
    os.path.join("Service Worker", "ScriptCache"),
]

# ── Chrome binary selection ────────────────────────────────────────────────────
# Priority: 1) real google-chrome-stable (undetectable), 2) full Chromium for
# Testing, 3) patchright headless-shell (last resort — Google detects it).
_REAL_CHROME = "/usr/bin/google-chrome-stable"
_FULL_CHROME = (
    "/root/.cache/ms-playwright/ms-playwright/"
    "chromium-1228/chrome-linux64/chrome"
)
_HEADLESS_SHELL = (
    "/root/.cache/ms-playwright/"
    "chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell"
)

import os as _os
# Use real Chrome if available; fall back gracefully
CHROME_EXECUTABLE: str | None = (
    _REAL_CHROME      if _os.path.isfile(_REAL_CHROME) else
    _FULL_CHROME      if _os.path.isfile(_FULL_CHROME) else
    _HEADLESS_SHELL   if _os.path.isfile(_HEADLESS_SHELL) else
    None  # let patchright auto-detect
)

# UA matching the installed Chrome version
# Update this when google-chrome-stable is updated on the worker.
_CHROME_VERSION = "153.0.8010.47"  # google-chrome-stable current
_STEALTH_UA_CHROME = (
    f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROME_VERSION} Safari/537.36"
)


# Chromium launch args (hardened, stealth, Colab-compatible)
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu-sandbox",
    "--ignore-gpu-blocklist",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--window-size=1920,1080",
    "--remote-debugging-address=0.0.0.0",  # bind to Tailscale IP
    # Anti-detection: language + feature flags
    "--disable-features=IsolateOrigins,site-per-process",
    "--lang=en-US,en",
    "--accept-lang=en-US,en;q=0.9",
    # Needed for real Chrome on Xvfb (no real GPU)
    "--use-gl=swiftshader",
    "--disable-software-rasterizer",
    # ── UA override: remove "HeadlessChrome" — Google detects it ──────────────
    # Real Chrome/Linux UA; version matches patchright Chromium 151.x
    (
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.7922.34 Safari/537.36"
    ),
]

# ── Stealth init script (ported from fingerprint.py / XIOBR applyFingerprintOverrides) ──
# Applied to every context via context.add_init_script() so it runs before any page JS.
_STEALTH_JS = r"""
(function () {
  'use strict';

  // 1. navigator.webdriver → undefined  (most critical check)
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
  } catch (_) {}

  // 2. Remove CDP residual keys left by Chrome devtools protocol
  const _cdcKeys = Object.getOwnPropertyNames(window).filter(k => k.match(/^cdc_/));
  _cdcKeys.forEach(k => { try { delete window[k]; } catch (_) {} });

  // 3. navigator.plugins — spoof 3 real Chrome built-ins
  const _fakePlugins = [
    { name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer',         description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer',  filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client',      filename: 'internal-nacl-plugin',        description: '' },
  ];
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => {
        const arr = _fakePlugins.map(p => Object.assign(Object.create(Plugin.prototype), p));
        Object.defineProperty(arr, 'item',   { value: (i) => arr[i] });
        Object.defineProperty(arr, 'namedItem', { value: (n) => arr.find(p => p.name === n) });
        Object.defineProperty(arr, 'length', { value: arr.length });
        return arr;
      },
      configurable: true,
    });
  } catch (_) {}

  // 4. window.chrome — required by many Google fingerprint checks
  if (!window.chrome) {
    Object.defineProperty(window, 'chrome', {
      value: {
        app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
               RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
        runtime: { OnInstalledReason: {}, OnRestartRequiredReason: {}, PlatformArch: {}, PlatformNaclArch: {}, PlatformOs: {},
                   RequestUpdateCheckStatus: {}, id: undefined },
        loadTimes: function() { return {}; },
        csi: function() { return {}; },
      },
      configurable: true, writable: false,
    });
  }

  // 5. navigator.languages
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true });
  } catch (_) {}

  // 6. navigator.userAgent — remove "HeadlessChrome", use real Chrome/Linux UA
  const _realUA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.8010.47 Safari/537.36';
  try { Object.defineProperty(navigator, 'userAgent', { get: () => _realUA, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'appVersion', { get: () => _realUA.replace('Mozilla/', ''), configurable: true }); } catch (_) {}
  // 6b. navigator.hardwareConcurrency / deviceMemory / platform
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64', configurable: true }); } catch (_) {}

  // 7. Screen dimensions (match --window-size=1920,1080)
  try { Object.defineProperty(screen, 'width',       { get: () => 1920, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'height',      { get: () => 1080, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availWidth',  { get: () => 1920, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availHeight', { get: () => 1040, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'colorDepth',  { get: () => 24, configurable: true }); } catch (_) {}
  try { Object.defineProperty(window, 'devicePixelRatio', { get: () => 1, configurable: true }); } catch (_) {}

  // 8. WebGL vendor/renderer spoofing
  const _getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Google Inc. (Google)';     // UNMASKED_VENDOR_WEBGL
    if (param === 37446) return 'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)';
    return _getParam.call(this, param);
  };
  try {
    const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return 'Google Inc. (Google)';
      if (param === 37446) return 'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)';
      return _getParam2.call(this, param);
    };
  } catch (_) {}
})();
"""



# ── Session registry ───────────────────────────────────────────────────────────
# session_id → {"browser": Browser, "pid": int, "port": int,
#               "cdp_ws_url": str, "proxy_url": str|None,
#               "profile_dir": str|None, "guard_task": Task}
_sessions: dict[str, dict[str, Any]] = {}


# ── Pydantic models ────────────────────────────────────────────────────────────
class LaunchRequest(BaseModel):
    session_id:  str
    proxy_url:   str | None = None
    profile_dir: str | None = None   # already-extracted local dir (or None = fresh)
    fingerprint: dict[str, Any] = {}
    headless:    bool = False   # False = Xvfb headed (vastly more undetectable)


class TerminateRequest(BaseModel):
    session_id: str


class PullProfileRequest(BaseModel):
    identity_id:      str
    # Accept both names: drive_object_key (new canonical) and r2_object_key (deprecated)
    drive_object_key: str | None = None
    r2_object_key:    str | None = None  # deprecated — use drive_object_key

    @property
    def object_key(self) -> str:
        """Resolve whichever key name the caller used."""
        key = self.drive_object_key or self.r2_object_key or ""
        if not key:
            raise ValueError("Either drive_object_key or r2_object_key must be provided")
        return key


class PushProfileRequest(BaseModel):
    identity_id:      str
    local_dir:        str
    # Accept both names: drive_object_key (new canonical) and r2_object_key (deprecated)
    drive_object_key: str | None = None
    r2_object_key:    str | None = None  # deprecated — use drive_object_key

    @property
    def object_key(self) -> str:
        """Resolve whichever key name the caller used."""
        key = self.drive_object_key or self.r2_object_key or ""
        if not key:
            raise ValueError("Either drive_object_key or r2_object_key must be provided")
        return key


# ── Drive FUSE helpers ─────────────────────────────────────────────────────────
def _drive_path(object_key: str) -> Path:
    """Resolve an object key to its full Drive FUSE path."""
    return Path(DRIVE_ROOT) / object_key


def _trim_profile(path: Path) -> None:
    for rel in TRIM_DIRS:
        full = path / rel
        if full.exists():
            shutil.rmtree(full, ignore_errors=True)


# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {"ok": True, "node": NODE_NAME, "active_sessions": len(_sessions)}


@app.get("/sessions")
async def list_sessions() -> dict:
    return {
        "sessions": [
            {
                "session_id": sid,
                "cdp_ws_url": info["cdp_ws_url"],
                "pid":        info["pid"],
            }
            for sid, info in _sessions.items()
        ]
    }


@app.post("/launch")
async def launch_browser(req: LaunchRequest) -> dict:
    """Launch patchright Chromium for a session. Returns CDP WebSocket URL."""
    from patchright.async_api import async_playwright  # noqa

    if req.session_id in _sessions:
        # Idempotent: return existing
        info = _sessions[req.session_id]
        return {"cdp_ws_url": info["cdp_ws_url"], "pid": info["pid"], "port": info["port"]}

    profile_dir = req.profile_dir

    # Pre-allocate a free port
    import socket as _sock
    with _sock.socket() as _s:
        _s.bind(("", 0))
        port = _s.getsockname()[1]

    # Build Chrome args
    args = [a for a in CHROMIUM_ARGS if not a.startswith("--user-data-dir")]
    if req.headless:
        args.append("--headless=new")
    if req.proxy_url:
        args.append(f"--proxy-server={req.proxy_url}")
    if profile_dir:
        args.append(f"--user-data-dir={profile_dir}")
    args.append(f"--remote-debugging-port={port}")

    tailscale_ip = _my_tailscale_ip()
    cdp_http_url = f"http://localhost:{port}"
    cdp_ws_url   = f"ws://{tailscale_ip}:{port}"

    pw = await async_playwright().start()
    browser = None
    context = None
    chrome_proc = None

    # ── Strategy A: Subprocess launch of real Chrome (undetected-chromedriver approach)
    # Bypasses patchright's binary lock → google-chrome-stable has no HeadlessChrome UA.
    if CHROME_EXECUTABLE and CHROME_EXECUTABLE != _HEADLESS_SHELL:
        logger.info(f"chrome_launch=subprocess binary={CHROME_EXECUTABLE} headless={req.headless}")
        import subprocess as _sp, time as _time

        env = dict(os.environ)
        env.setdefault("DISPLAY", ":99")
        chrome_cmd = [CHROME_EXECUTABLE] + args
        chrome_proc = _sp.Popen(
            chrome_cmd, env=env,
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
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
            await pw.stop()
            raise HTTPException(status_code=500, detail="Chrome CDP did not become ready")

        try:
            browser = await pw.chromium.connect_over_cdp(cdp_http_url)
            # After CDP connect, get or create context
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = await browser.new_context(user_agent=_STEALTH_UA_CHROME)
        except Exception as exc:
            chrome_proc.kill()
            await pw.stop()
            raise HTTPException(status_code=500, detail=f"CDP connect failed: {exc}") from exc

        pid = chrome_proc.pid

    else:
        # ── Strategy B: patchright managed launch (fallback — headless-shell)
        logger.info(f"chrome_launch=patchright headless={req.headless}")
        try:
            pw_args = [a for a in args if not a.startswith("--user-data-dir")]
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
        "browser":      browser,
        "context":      context,
        "pw":           pw,
        "pid":          pid,
        "port":         port,
        "cdp_ws_url":   cdp_ws_url,
        "proxy_url":    req.proxy_url,
        "profile_dir":  profile_dir,
        "chrome_proc":  chrome_proc,   # subprocess.Popen for real Chrome; None if patchright-managed
        "guard_task":   None,
    }

    # ── Apply stealth patches to the context ──────────────────────────────────
    # init_script runs before any page JS — covers webdriver flag, CDP keys,
    # window.chrome, plugins, WebGL, etc. (all 8 XIOBR fingerprint layers)
    try:
        _ctx = context or (browser.contexts[0] if browser and browser.contexts else None)
        if _ctx:
            await _ctx.add_init_script(_STEALTH_JS)
            logger.info(f"stealth_init_applied session={req.session_id}")
    except Exception as _se:
        logger.warning(f"stealth_init_failed session={req.session_id} err={_se}")

    # ── Crash watcher ──────────────────────────────────────────────────────────
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
                        json={"session_id": req.session_id, "node": NODE_NAME, "reason": "browser_disconnected"},
                        headers={"Authorization": f"Bearer {XIOSYNC_TOKEN}"},
                    )
            except Exception as exc:
                logger.warning(f"browser-crashed notify failed: {exc}")

    # Attach crash watcher to whichever object supports .on("disconnected")
    _watch_target = browser if browser else (context.browser if context and context.browser else None)
    if _watch_target:
        _watch_target.on("disconnected", lambda: asyncio.create_task(_on_browser_disconnected()))

    if req.proxy_url:
        task = asyncio.create_task(
            _proxy_probe_loop(req.session_id, req.proxy_url),
            name=f"proxy-guard-{req.session_id[:8]}",
        )
        _sessions[req.session_id]["guard_task"] = task

    logger.info(f"launched session={req.session_id} port={port} profile={profile_dir} cdp={cdp_ws_url}")
    return {"cdp_ws_url": cdp_ws_url, "pid": pid, "port": port}


@app.post("/terminate")
async def terminate_browser(req: TerminateRequest) -> dict:
    """Kill the Chromium process for a session."""
    info = _sessions.pop(req.session_id, None)
    if info is None:
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

    logger.info(f"terminated session={req.session_id}")
    return {"ok": True}


@app.post("/pull-profile")
async def pull_profile(req: PullProfileRequest) -> dict:
    """Fetch Chrome profile tar.gz from Drive FUSE and extract locally.

    Returns local profile dir path, or 404 if not found on Drive.
    """
    slug = req.identity_id.replace("-", "")[:16]
    local_dir = PROFILES_BASE / f"PRFL_{slug}__{NODE_NAME}"

    if local_dir.exists() and any(local_dir.iterdir()):
        logger.info(f"pull-profile cache-hit identity={req.identity_id} → {local_dir}")
        return {"profile_dir": str(local_dir)}

    # Read from Drive FUSE
    drive_file = _drive_path(req.object_key)
    if not drive_file.exists():
        raise HTTPException(status_code=404, detail=f"Profile not on Drive: {req.object_key}")

    try:
        tar_bytes = drive_file.read_bytes()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Drive read failed: {exc}") from exc

    # Extract into a temp staging dir to avoid partial-write races
    import uuid as _uuid
    stage_dir = PROFILES_BASE / f"_stage_{_uuid.uuid4().hex[:8]}"
    stage_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(tar_bytes)
        tmp_path = tmp.name
    try:
        with tarfile.open(tmp_path, "r:gz") as tf:
            tf.extractall(path=str(stage_dir))
    finally:
        os.unlink(tmp_path)

    # The tarball root dir name may differ from local_dir — find it and rename
    extracted_children = [p for p in stage_dir.iterdir() if p.is_dir()]
    if not extracted_children:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Tarball extracted no directories")

    extracted_root = extracted_children[0]
    if local_dir.exists():
        shutil.rmtree(local_dir, ignore_errors=True)
    shutil.move(str(extracted_root), str(local_dir))
    shutil.rmtree(stage_dir, ignore_errors=True)

    _trim_profile(local_dir)

    logger.info(f"pull-profile identity={req.identity_id} key={req.object_key} → {local_dir}")
    return {"profile_dir": str(local_dir)}


@app.post("/push-profile")
async def push_profile(req: PushProfileRequest) -> dict:
    """Tar local Chrome profile dir and write back to Drive FUSE."""
    profile_path = Path(req.local_dir)
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail=f"Profile dir not found: {req.local_dir}")

    _trim_profile(profile_path)

    drive_file = _drive_path(req.object_key)
    drive_file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with tarfile.open(tmp_path, "w:gz") as tf:
            tf.add(str(profile_path), arcname=profile_path.name)
        tar_bytes = Path(tmp_path).read_bytes()
        # Atomic-ish: write to tmp alongside target, then replace
        drive_tmp = drive_file.with_suffix(".tar.gz.tmp")
        drive_tmp.write_bytes(tar_bytes)
        drive_tmp.replace(drive_file)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    logger.info(f"push-profile identity={req.identity_id} key={req.object_key} "
                f"size={len(tar_bytes)}")
    return {"ok": True, "size_bytes": len(tar_bytes)}


# ── Proxy liveness probe ───────────────────────────────────────────────────────
async def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _proxy_probe_loop(session_id: str, proxy_url: str) -> None:
    """Probe SOCKS5 proxy every 10s. On 2 consecutive failures → report + kill."""
    m = re.match(r"socks5://([^:]+):(\d+)", proxy_url)
    if not m:
        return
    host, port = m.group(1), int(m.group(2))
    failures = 0
    THRESHOLD = 2

    while True:
        await asyncio.sleep(10)
        if session_id not in _sessions:
            return

        ok = await _tcp_probe(host, port)
        if ok:
            failures = 0
        else:
            failures += 1
            logger.warning(f"proxy probe fail session={session_id} consecutive={failures}")
            if failures >= THRESHOLD:
                logger.error(f"EXIT NODE LOST for session={session_id} — hard kill")
                # Kill locally
                info = _sessions.pop(session_id, None)
                if info:
                    try:
                        await info["browser"].close()
                    except Exception:
                        pass
                    try:
                        await info["pw"].stop()
                    except Exception:
                        pass

                # Notify XIOSYNC control plane (best-effort)
                if XIOSYNC_BASE and XIOSYNC_TOKEN:
                    try:
                        import httpx  # noqa
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            await client.post(
                                f"{XIOSYNC_BASE}/api/v1/internal/xiorun/proxy-lost",
                                json={"session_id": session_id, "node": NODE_NAME},
                                headers={"Authorization": f"Bearer {XIOSYNC_TOKEN}"},
                            )
                    except Exception as exc:
                        logger.warning(f"proxy-lost notify failed: {exc}")
                return


# ── Tailscale IP helper ────────────────────────────────────────────────────────
_my_ts_ip: str | None = None


def _my_tailscale_ip() -> str:
    global _my_ts_ip
    if _my_ts_ip:
        return _my_ts_ip
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        ip = result.stdout.strip()
        if ip:
            _my_ts_ip = ip
            return ip
    except Exception:
        pass
    return "127.0.0.1"


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
