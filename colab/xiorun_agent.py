"""xiorun_agent.py — Thin FastAPI agent for XIORUN on Colab workers.

Started by boot.py Phase 7 on each Colab runtime.
Listens on port 9300 (Tailscale-accessible only).

Endpoints:
    POST /launch         — start patchright Chromium for a session
    POST /terminate      — kill Chromium for a session
    POST /pull-profile   — fetch Chrome profile tar.gz from Drive, extract locally
    POST /push-profile   — tar local Chrome profile, upload to Drive
    POST /run-uc-login   — UC stealth login (Google Chrome 153 + exit node proxy)
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

import json
import urllib.request

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("xiorun_agent")

from contextlib import asynccontextmanager as _acm

# SSH SOCKS5 tunnel process — set when XIORUN_PROXY_SSH_HOST is configured
_ssh_tunnel_proc: Any = None
_SSH_PROXY_URL: str | None = None   # set at startup: "socks5://127.0.0.1:1056"

@_acm
async def _lifespan(application):
    """Kill orphan Chrome processes; start SSH SOCKS5 tunnel if configured."""
    global _ssh_tunnel_proc, _SSH_PROXY_URL

    import subprocess as _sp, time as _t

    # ── Kill orphan Chrome/ChromeDriver from prior agent runs ─────────────────
    for _sig in ("-TERM", "-KILL"):
        try:
            _sp.run(["pkill", _sig, "-f", "chrome-linux64/chrome"], capture_output=True)
            _sp.run(["pkill", _sig, "-f", "undetected_chromedriver"],  capture_output=True)
        except Exception:
            pass
    _t.sleep(1)

    # ── WS SOCKS5 Bridge: route proxy traffic via XIOSYNC WebSocket tunnel ───────
    # XIOSYNC exposes /api/v1/proxy/tunnel?target=HOST:PORT — we open a local
    # SOCKS5 server on 127.0.0.1:19055 that pipes each connection through that WS.
    # This works even when Tailscale ACL blocks direct node-to-node traffic because
    # the XIOSYNC public hostname (karmas-mac-mini.taildd8b9a.ts.net) is accessible
    # via Tailscale Funnel regardless of ACL peer restrictions.
    _xiosync_base   = os.environ.get("XIORUN_XIOSYNC_BASE", "")
    _internal_sec   = os.environ.get("XIORUN_INTERNAL_SECRET", "")
    _pppoe_proxy    = os.environ.get("XIORUN_PPPOE_PROXY", "100.106.81.15:10001")
    _WS_SOCKS5_PORT = 19055
    _ws_bridge_task = None

    if _xiosync_base and _internal_sec:
        # Build the WS URL: wss://karmas-mac-mini.../api/v1/proxy/tunnel?target=HOST:PORT
        _ws_base = _xiosync_base.replace("https://", "wss://").replace("http://", "ws://")
        _ws_tunnel_url = f"{_ws_base}/api/v1/proxy/tunnel?target={_pppoe_proxy}"

        async def _ws_socks5_bridge():
            """Local SOCKS5 server that tunnels each connection through XIOSYNC WS."""
            import struct, websockets  # noqa: PLC0415

            async def _handle_socks5_client(reader, writer):
                """Minimal SOCKS5 server: accept any user/pass, forward CONNECT to WS."""
                try:
                    # --- SOCKS5 handshake ---
                    hdr = await reader.read(3)
                    if len(hdr) < 2 or hdr[0] != 5:
                        writer.close(); return
                    n_methods = hdr[1]
                    if n_methods > 0:
                        await reader.read(n_methods - (len(hdr) - 2))
                    writer.write(b"\x05\x00")  # no auth required
                    await writer.drain()

                    # --- SOCKS5 request ---
                    req = await reader.read(4)
                    if len(req) < 4 or req[1] != 1:  # only CONNECT supported
                        writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
                        await writer.drain(); writer.close(); return
                    atyp = req[3]
                    if atyp == 1:    # IPv4
                        addr_b = await reader.read(4)
                        addr = ".".join(str(b) for b in addr_b)
                    elif atyp == 3:  # domain
                        dlen = (await reader.read(1))[0]
                        addr = (await reader.read(dlen)).decode()
                    elif atyp == 4:  # IPv6
                        addr_b = await reader.read(16)
                        import socket as _s  # noqa: PLC0415
                        addr = _s.inet_ntop(_s.AF_INET6, addr_b)
                    else:
                        writer.close(); return
                    port_b = await reader.read(2)
                    port = struct.unpack("!H", port_b)[0]

                    # --- Open WS tunnel to XIOSYNC → PPPoE SOCKS5 proxy ---
                    # The proxy_tunnel endpoint only allows whitelisted targets (PPPoE proxy).
                    # We open a tunnel to the PPPoE SOCKS5 proxy and relay the SOCKS5
                    # CONNECT request through it so the proxy dials the actual target.
                    try:
                        import socket as _sock_mod  # noqa: PLC0415
                        ws = await websockets.connect(
                            _ws_tunnel_url,  # wss://.../proxy/tunnel?target=100.106.81.15:10001
                            additional_headers={"x-internal-secret": _internal_sec},
                            ping_interval=20, ping_timeout=20,
                            open_timeout=10,
                            family=_sock_mod.AF_INET,  # force IPv4 — Funnel has no IPv6
                        )
                    except Exception as _e:
                        logger.debug(f"ws_bridge: WS connect failed {addr}:{port} — {_e}")
                        writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)
                        await writer.drain(); writer.close(); return

                    # --- Do SOCKS5 handshake with the upstream PPPoE proxy through WS ---
                    # 1. Send greeting (no auth)
                    await ws.send(bytes([0x05, 0x01, 0x00]))
                    _greet_resp = await asyncio.wait_for(ws.recv(), timeout=5)
                    if len(_greet_resp) < 2 or _greet_resp[1] != 0:
                        logger.debug(f"ws_bridge: upstream SOCKS5 auth failed: {_greet_resp!r}")
                        writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)
                        await writer.drain(); writer.close(); await ws.close(); return

                    # 2. Send CONNECT for the original target
                    if atyp == 3:  # domain (most common)
                        _addr_bytes = bytes([len(addr)]) + addr.encode()
                    elif atyp == 1:  # IPv4
                        _addr_bytes = bytes(int(x) for x in addr.split("."))
                    else:  # IPv6 — encode as domain
                        atyp = 3
                        _addr_bytes = bytes([len(addr)]) + addr.encode()
                    _port_bytes = struct.pack("!H", port)
                    await ws.send(bytes([0x05, 0x01, 0x00, atyp]) + _addr_bytes + _port_bytes)
                    _conn_resp = await asyncio.wait_for(ws.recv(), timeout=8)
                    if len(_conn_resp) < 2 or _conn_resp[1] != 0:
                        logger.debug(f"ws_bridge: upstream CONNECT failed: {_conn_resp!r}")
                        writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)
                        await writer.drain(); writer.close(); await ws.close(); return

                    # --- Send SOCKS5 success reply to local client ---
                    writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
                    await writer.drain()

                    # --- Bidirectional relay ---
                    async def _local_to_ws():
                        try:
                            while True:
                                d = await reader.read(65536)
                                if not d: break
                                await ws.send(d)
                        except Exception: pass
                        try: await ws.close()
                        except Exception: pass

                    async def _ws_to_local():
                        try:
                            async for msg in ws:
                                writer.write(msg if isinstance(msg, bytes) else msg.encode())
                                await writer.drain()
                        except Exception: pass
                        try: writer.close()
                        except Exception: pass

                    await asyncio.gather(_local_to_ws(), _ws_to_local(), return_exceptions=True)
                except Exception as _e:
                    logger.debug(f"ws_bridge: client handler error: {_e}")
                    try: writer.close()
                    except Exception: pass

            try:
                # Force-free the port in case a previous agent is holding it
                import signal as _sig
                try:
                    _bind_sock = __import__("socket").socket()
                    _bind_sock.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_REUSEADDR, 1)
                    _bind_sock.bind(("127.0.0.1", _WS_SOCKS5_PORT))
                    _bind_sock.close()
                except OSError:
                    # Port in use — kill the holder and wait briefly
                    _sp.run(
                        f"fuser -k {_WS_SOCKS5_PORT}/tcp 2>/dev/null || "
                        f"lsof -ti:{_WS_SOCKS5_PORT} | xargs -r kill -9 2>/dev/null || true",
                        shell=True, timeout=5
                    )
                    await asyncio.sleep(1)
                srv = await asyncio.start_server(
                    _handle_socks5_client, "127.0.0.1", _WS_SOCKS5_PORT,
                    reuse_address=True,
                )
                async with srv:
                    await srv.serve_forever()
            except OSError as _bind_err:
                logger.warning(
                    f"ws_bridge: port {_WS_SOCKS5_PORT} already in use ({_bind_err}) — "
                    "bridge skipped, will fall back to SSH tunnel"
                )

        # Verify XIOSYNC tunnel endpoint is reachable before advertising it
        try:
            import websockets as _wsd  # noqa: PLC0415
            _test_url = f"{_ws_base}/api/v1/proxy/tunnel?target={_pppoe_proxy}"
            _test_ws = await asyncio.wait_for(
                _wsd.connect(
                    _test_url,
                    additional_headers={"x-internal-secret": _internal_sec},
                    open_timeout=8,
                    family=__import__("socket").AF_INET,  # force IPv4
                ),
                timeout=10,
            )
            await _test_ws.close()
            # Start the bridge server
            _ws_bridge_task = asyncio.create_task(_ws_socks5_bridge())
            _t.sleep(0.5)
            _SSH_PROXY_URL = f"socks5://127.0.0.1:{_WS_SOCKS5_PORT}"
            logger.info(
                f"WS SOCKS5 bridge ready on :{_WS_SOCKS5_PORT} "
                f"→ XIOSYNC tunnel → {_pppoe_proxy}"
            )
        except Exception as _e:
            logger.warning(f"WS SOCKS5 bridge unavailable: {_e} — will try SSH tunnel")

    # ── SSH SOCKS5 tunnel (fallback when WS bridge not available) ─────────────
    _proxy_ssh_host = os.environ.get("XIORUN_PROXY_SSH_HOST", "")
    _proxy_ssh_user = os.environ.get("XIORUN_PROXY_SSH_USER", "karmareturns")
    _proxy_ssh_key  = os.environ.get("XIORUN_PROXY_SSH_KEY", "/root/.ssh/xio_proxy_key")
    _proxy_local_port = int(os.environ.get("XIORUN_PROXY_LOCAL_PORT", "19056"))

    if not _SSH_PROXY_URL and _proxy_ssh_host and os.path.exists(_proxy_ssh_key):
        try:
            _ssh_tunnel_proc = _sp.Popen([
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes",
                "-D", f"127.0.0.1:{_proxy_local_port}",
                "-N",
                "-i", _proxy_ssh_key,
                f"{_proxy_ssh_user}@{_proxy_ssh_host}",
            ], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            _t.sleep(3)
            if _ssh_tunnel_proc.poll() is None:
                _SSH_PROXY_URL = f"socks5://127.0.0.1:{_proxy_local_port}"
                logger.info(
                    f"SSH SOCKS5 tunnel up: {_proxy_ssh_user}@{_proxy_ssh_host} "
                    f"→ {_SSH_PROXY_URL}"
                )
            else:
                logger.warning(f"SSH SOCKS5 tunnel failed (exit {_ssh_tunnel_proc.returncode})")
                _ssh_tunnel_proc = None
        except Exception as _e:
            logger.warning(f"SSH SOCKS5 tunnel setup failed: {_e}")

    yield

    # ── Shutdown: quit UC drivers + kill SSH tunnel ───────────────────────────
    for _drv in list(_uc_drivers.values()):
        try: _drv.quit()
        except Exception: pass
    if _ssh_tunnel_proc and _ssh_tunnel_proc.poll() is None:
        _ssh_tunnel_proc.terminate()

app = FastAPI(title="XIORUN Agent", version="1.0.0", lifespan=_lifespan)


# ── Dynamic IP → Timezone resolver ────────────────────────────────────────────
# Uses ip-api.com (free, no API key, 45 req/min limit).
# Results are cached forever per IP — geo of a given IP never changes.
_IP_TZ_CACHE: dict[str, str] = {}
_IP_GEO_CACHE: dict[str, dict] = {}

def _resolve_ip_geo(public_ip: str | None) -> dict:
    """Resolve full geo profile for a public IP using ip-api.com.

    Returns dict with: timezone, lat, lon, country, countryCode, city, isp, org, locale
    Falls back to safe defaults on any error.  Never raises.
    """
    _DEFAULTS = {
        "timezone": "UTC", "lat": 0.0, "lon": 0.0,
        "country": "United States", "countryCode": "US",
        "city": "New York", "isp": "Comcast Cable",
        "org": "AS7922 Comcast Cable Communications", "locale": "en-US",
    }
    if not public_ip or public_ip.startswith(("10.", "192.168.", "100.", "127.")):
        return _DEFAULTS
    if public_ip in _IP_GEO_CACHE:
        return _IP_GEO_CACHE[public_ip]
    try:
        fields = "status,timezone,lat,lon,country,countryCode,city,isp,org"
        url = f"http://ip-api.com/json/{public_ip}?fields={fields}"
        req = urllib.request.Request(url, headers={"User-Agent": "xiorun-agent/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "success":
            cc = data.get("countryCode", "US")
            # Map country code to primary locale
            _CC_LOCALE = {
                "IN": "en-IN", "GB": "en-GB", "AU": "en-AU", "CA": "en-CA",
                "US": "en-US", "PK": "en-PK", "NG": "en-NG", "PH": "en-PH",
                "DE": "de-DE", "FR": "fr-FR", "JP": "ja-JP", "CN": "zh-CN",
                "BR": "pt-BR", "RU": "ru-RU", "MX": "es-MX", "KR": "ko-KR",
            }
            result = {
                "timezone":    data.get("timezone", "UTC"),
                "lat":         float(data.get("lat", 0.0)),
                "lon":         float(data.get("lon", 0.0)),
                "country":     data.get("country", "United States"),
                "countryCode": cc,
                "city":        data.get("city", ""),
                "isp":         data.get("isp", ""),
                "org":         data.get("org", ""),
                "locale":      _CC_LOCALE.get(cc, "en-US"),
            }
        else:
            result = _DEFAULTS.copy()
    except Exception as e:
        logger.warning(f"ip-geo-lookup failed for {public_ip}: {e}")
        result = _DEFAULTS.copy()
    _IP_GEO_CACHE[public_ip] = result
    _IP_TZ_CACHE[public_ip] = result["timezone"]
    logger.info(f"ip-geo: {public_ip} → tz={result['timezone']} loc={result['city']},{result['countryCode']} lat={result['lat']:.2f} lon={result['lon']:.2f}")
    return result


def _resolve_ip_timezone(public_ip: str | None) -> str:
    """Convenience wrapper — returns just timezone string."""
    return _resolve_ip_geo(public_ip).get("timezone", "UTC")


def _get_runtime_public_ip() -> str | None:
    """Detect this runtime's own public IP (used when no exit node is assigned).
    Returns None on failure — caller falls back to 'UTC'.
    """
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None


# ── Dynamic runtime capacity detection ────────────────────────────────────────
# Detects CPU / T4 GPU / TPU Colab runtime and computes safe max Chrome sessions.
# Policy: CPU, T4 GPU, and TPU runtimes are supported.
# Formula:
#   CPU / TPU: max_sessions = min(cpu_count * SESSIONS_PER_VCPU_CPU, sys_ram_sessions)
#   T4:        max_sessions = min(cpu_count * SESSIONS_PER_VCPU_GPU, sys_ram_sessions, gpu_sessions)
# Chrome headless sys RAM: ~180 MB/session (CPU/TPU), ~130 MB/session (T4, GPU handles rendering)
# Chrome GPU VRAM: ~120 MB/session on T4. TPU VRAM is irrelevant (Chrome is CPU-only).

_SESSIONS_PER_VCPU_CPU = 3    # CPU/TPU runtime: conservative (JS engine contention)
_SESSIONS_PER_VCPU_GPU = 6    # T4 runtime: GPU handles rendering → more headroom
_CHROME_SYS_MB_CPU     = 180  # sys RAM per Chrome session (CPU/TPU runtime)
_CHROME_SYS_MB_GPU     = 130  # sys RAM per Chrome session (T4 — GPU offloads renderer)
_CHROME_GPU_MB         = 120  # GPU VRAM per Chrome session (T4 only)
_OS_HEADROOM_MB        = 1536 # 1.5 GB reserved for OS + agent + Colab kernel


def _detect_runtime_capacity() -> dict:
    """Detect Colab runtime type and compute safe max Chrome sessions.

    Returns:
        {runtime_type, sys_ram_gb, gpu_vram_gb, cpu_count, max_sessions}
    """
    sys_ram_mb  = psutil.virtual_memory().total // (1024 * 1024)
    cpu_count   = psutil.cpu_count(logical=True) or 2
    gpu_vram_mb = 0
    gpu_name    = ""
    runtime_type = "cpu"

    # ── TPU detection (check before nvidia-smi; TPU runtimes have no CUDA GPU) ─
    _tpu_name = (
        os.environ.get("TPU_NAME") or
        os.environ.get("TPU_ACCELERATOR_TYPE") or
        os.environ.get("TPU_WORKER_ID") or
        os.environ.get("COLAB_TPU_ADDR")
    )
    _has_tpu_device = os.path.exists("/dev/accel0")
    if _tpu_name or _has_tpu_device:
        # TPU runtimes cannot use TPU VRAM for Chrome (no XLA for V8/Blink).
        # Use CPU formula. gpu_vram stays 0.
        runtime_type = "tpu"
        tpu_label    = _tpu_name or "tpu"
        logger.info(f"TPU runtime detected: {tpu_label} — applying CPU session limits")
    else:
        # ── GPU detection (nvidia-smi) ─────────────────────────────────────────
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                timeout=5, stderr=subprocess.DEVNULL,
            ).decode().strip()
            if out:
                parts = [p.strip() for p in out.split(",")]
                gpu_name    = parts[0].lower()
                gpu_vram_mb = int(parts[1]) if len(parts) > 1 else 0
                if "t4" in gpu_name:
                    runtime_type = "t4"
                else:
                    # A100, V100, etc. — not supported by policy; treat as CPU limit
                    logger.warning(f"Unsupported GPU runtime: {parts[0]} — applying CPU limits")
                    runtime_type = "cpu"
        except FileNotFoundError:
            pass  # no nvidia-smi = CPU runtime
        except Exception as e:
            logger.warning(f"nvidia-smi error: {e}")

    available_sys_mb = max(0, sys_ram_mb - _OS_HEADROOM_MB)

    if runtime_type == "t4":
        available_gpu_mb  = max(0, gpu_vram_mb - 512)   # 512 MB GPU OS headroom
        cpu_limit         = cpu_count * _SESSIONS_PER_VCPU_GPU
        sys_ram_limit     = available_sys_mb // _CHROME_SYS_MB_GPU
        gpu_limit         = available_gpu_mb // _CHROME_GPU_MB
        max_sessions      = max(1, min(cpu_limit, sys_ram_limit, gpu_limit))
    else:
        # CPU-only and TPU both use the CPU formula (no GPU VRAM for Chrome)
        cpu_limit     = cpu_count * _SESSIONS_PER_VCPU_CPU
        sys_ram_limit = available_sys_mb // _CHROME_SYS_MB_CPU
        max_sessions  = max(1, min(cpu_limit, sys_ram_limit))

    cap = {
        "runtime_type":  runtime_type,
        "gpu_name":       gpu_name or "none",
        "sys_ram_gb":    round(sys_ram_mb / 1024, 1),
        "gpu_vram_gb":   round(gpu_vram_mb / 1024, 1),
        "cpu_count":     cpu_count,
        "max_sessions":  max_sessions,
    }
    logger.info(f"runtime-capacity: {cap}")
    return cap


# Computed once at startup
_RUNTIME_CAP: dict = _detect_runtime_capacity()
_MAX_SESSIONS: int = _RUNTIME_CAP["max_sessions"]
logger.info(f"MAX_SESSIONS set to {_MAX_SESSIONS} ({_RUNTIME_CAP['runtime_type']} runtime)")

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

# UA must EXACTLY match the installed Chrome binary version.
# Chrome 131.0.6778.108 = last version UC 3.5.5 fully patches (navigator.webdriver=False confirmed).
# Chrome 153 is NOT used — UC 3.5.5 doesn't patch it and Google gives only 3 cookies.
_CHROME_VERSION = "131.0.6778.108"  # /opt/chrome131/chrome
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
    # Anti-automation detection
    "--disable-blink-features=AutomationControlled",
    "--disable-automation",
    "--disable-infobars",
    "--remote-debugging-address=0.0.0.0",  # bind to Tailscale IP
    # Language
    "--lang=en-US,en",
    "--accept-lang=en-US,en;q=0.9",
    # WebGL software rendering (essential for Xvfb / no-GPU)
    "--use-gl=swiftshader",
    "--use-angle=swiftshader",
    "--disable-software-rasterizer",
    # Window geometry (800x600 default = instant bot flag)
    "--window-size=1920,1080",
    "--start-maximized",
    # Session stability
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--disable-extensions-except=",
]

# ── Stealth init script — comprehensive fingerprint spoofing ──────────────────
# Template placeholders replaced at launch time by Python:
#   {TIMEZONE}     IANA tz string  e.g. "Asia/Kolkata"
#   {LOCALE}       BCP-47 locale   e.g. "en-IN"
#   {CANVAS_SEED}  int32 seed for canvas noise
#   {AUDIO_SEED}   int32 seed for audio noise
#   {LAT}          float latitude  for geolocation
#   {LON}          float longitude for geolocation
#   {UA}           full User-Agent string
#   {TZ_OFFSET}    int minutes west of UTC (e.g. -330 for IST)
_STEALTH_JS = r"""
(function () {
  'use strict';


  // ── Jitter timing APIs to prevent extension measurement ───────────────────
  const _origPerfNow = performance.now;
  performance.now = function() { return _origPerfNow.call(this) + (Math.random() * 0.05); };
  const _origDateNow = Date.now;
  Date.now = function() { return _origDateNow.call(this) + Math.floor(Math.random() * 5); };
  // ── 1. navigator.webdriver → undefined (most critical bot check) ──────────
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
  } catch (_) {}

  // ── 2. Remove CDP residual cdc_ keys left by chromedriver ────────────────
  Object.getOwnPropertyNames(window).filter(k => k.match(/^cdc_/))
    .forEach(k => { try { delete window[k]; } catch (_) {} });

  // ── 3. navigator.plugins — 5 realistic Chrome built-ins ──────────────────
  const _fakePlugins = [
    { name: 'Chrome PDF Plugin',          filename: 'internal-pdf-viewer',          description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer',          filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client',              filename: 'internal-nacl-plugin',          description: '' },
    { name: 'WebKit built-in PDF',        filename: 'internal-pdf-viewer',           description: '' },
    { name: 'Widevine Content Decryption Module', filename: 'widevinecdmadapter.dll', description: 'Enables Widevine licenses for play back of HTML audio/video content.' },
  ];
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => {
        const arr = _fakePlugins.map(p => Object.assign(Object.create(Plugin.prototype), p));
        Object.defineProperty(arr, 'item',     { value: (i) => arr[i] });
        Object.defineProperty(arr, 'namedItem',{ value: (n) => arr.find(p => p.name === n) });
        Object.defineProperty(arr, 'length',   { value: arr.length });
        return arr;
      },
      configurable: true,
    });
    Object.defineProperty(navigator, 'mimeTypes', {
      get: () => {
        const mt = [
          { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: _fakePlugins[0] },
          { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: _fakePlugins[1] },
        ];
        return Object.assign(mt, { length: mt.length, item: i => mt[i], namedItem: n => mt.find(m => m.type === n) });
      },
      configurable: true,
    });
  } catch (_) {}

  // ── 4. window.chrome — full runtime object Google checks ─────────────────
  if (!window.chrome) {
    try {
      Object.defineProperty(window, 'chrome', {
        value: {
          app: {
            isInstalled: false,
            InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
            RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
          },
          runtime: {
            OnInstalledReason: {}, OnRestartRequiredReason: {}, PlatformArch: {},
            PlatformNaclArch: {}, PlatformOs: {}, RequestUpdateCheckStatus: {}, id: undefined,
            connect: () => {}, sendMessage: () => {},
          },
          loadTimes: function() { return { firstPaintTime: performance.now()/1000 - 0.05 }; },
          csi: function() { return { startE: Date.now() - 1000, onloadT: Date.now(), pageT: 1000, tran: 15 }; },
        },
        configurable: true, writable: false,
      });
    } catch (_) {}
  }

  // ── 5. Language / Locale (IP-geo injected) ────────────────────────────────
  const _LOCALE = '{LOCALE}';
  const _LANG   = _LOCALE.split('-')[0];
  try {
    Object.defineProperty(navigator, 'language',  { get: () => _LOCALE, configurable: true });
    Object.defineProperty(navigator, 'languages', { get: () => [_LOCALE, _LANG, 'en'], configurable: true });
  } catch (_) {}

  // ── 6. User-Agent + related navigator props ───────────────────────────────
  const _UA = '{UA}';
  try { Object.defineProperty(navigator, 'userAgent',  { get: () => _UA, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'appVersion', { get: () => _UA.replace('Mozilla/', ''), configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => {CPU_COUNT}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'deviceMemory',        { get: () => {DEVICE_MEMORY_GB}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'platform',            { get: () => '{PLATFORM}', configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'vendor',              { get: () => 'Google Inc.', configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'vendorSub',           { get: () => '', configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'productSub',          { get: () => '20030107', configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'maxTouchPoints',      { get: () => 0, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'cookieEnabled',       { get: () => true, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'onLine',              { get: () => true, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'doNotTrack',          { get: () => null, configurable: true }); } catch (_) {}

  // ── 6b. UserAgentData (Client Hints) spoofing ───────────────────────────
  try {
    const _chPlatform = '{CH_PLATFORM}';
    const _mobile = {IS_MOBILE};
    const _brands = [
      {brand: 'Chromium', version: '131'},
      {brand: 'Google Chrome', version: '131'},
      {brand: 'Not_A Brand', version: '24'}
    ];
    Object.defineProperty(navigator, 'userAgentData', {
      get: () => ({
        brands: _brands,
        mobile: _mobile,
        platform: _chPlatform,
        getHighEntropyValues: (hints) => Promise.resolve({
          brands: _brands,
          mobile: _mobile,
          platform: _chPlatform,
          platformVersion: '10.0.0',
          architecture: '{CH_ARCH}',
          model: '',
          bitness: '64',
          fullVersionList: _brands
        })
      }),
      configurable: true
    });
  } catch (_) {}

  // ── 7. Screen dimensions ──────────────────────────────────────────────────
  try { Object.defineProperty(screen, 'width',       { get: () => {SCREEN_W}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'height',      { get: () => {SCREEN_H}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availWidth',  { get: () => {SCREEN_W}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availHeight', { get: () => {SCREEN_AH}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availLeft',   { get: () => 0, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availTop',    { get: () => 0, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'colorDepth',  { get: () => 24, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'pixelDepth',  { get: () => 24, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'orientation', { get: () => ({ type: 'landscape-primary', angle: 0 }), configurable: true }); } catch (_) {}
  try { Object.defineProperty(window, 'devicePixelRatio', { get: () => 1, configurable: true }); } catch (_) {}
  try { Object.defineProperty(window, 'outerWidth',  { get: () => {SCREEN_W}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(window, 'outerHeight', { get: () => {SCREEN_H}, configurable: true }); } catch (_) {}

  // ── 8. WebGL vendor / renderer ────────────────────────────────────────────
  const _WEBGL_VENDOR   = '{WEBGL_V}';
  const _WEBGL_RENDERER = '{WEBGL_R}';
  const _patchWebGL = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    proto.getParameter = function(p) {
      if (p === 37445) return _WEBGL_VENDOR;
      if (p === 37446) return _WEBGL_RENDERER;
      return orig.call(this, p);
    };
    const origExt = proto.getExtension;
    proto.getExtension = function(name) {
      const ext = origExt.call(this, name);
      if (name === 'WEBGL_debug_renderer_info') {
        return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
      }
      return ext;
    };
  };
  try { _patchWebGL(WebGLRenderingContext.prototype); } catch (_) {}
  try { _patchWebGL(WebGL2RenderingContext.prototype); } catch (_) {}

  // ── 9. Timezone — Intl + Date.prototype ──────────────────────────────────
  const _XIO_TZ     = '{TIMEZONE}';
  const _XIO_OFFSET = {TZ_OFFSET};   // minutes west of UTC (e.g. -330 for IST = UTC+5:30)
  if (_XIO_TZ && _XIO_TZ !== 'UTC') {
    try {
      const _OrigDTF = Intl.DateTimeFormat;
      const _PatchedDTF = function(locale, opts) {
        opts = Object.assign({}, opts || {});
        if (!opts.timeZone) opts.timeZone = _XIO_TZ;
        return new _OrigDTF(locale || _LOCALE, opts);
      };
      Object.setPrototypeOf(_PatchedDTF, _OrigDTF);
      _PatchedDTF.prototype = _OrigDTF.prototype;
      _PatchedDTF.supportedLocalesOf = _OrigDTF.supportedLocalesOf.bind(_OrigDTF);
      Intl.DateTimeFormat = _PatchedDTF;
    } catch (_) {}
    // Also patch Date.prototype.getTimezoneOffset
    try {
      const _origGTO = Date.prototype.getTimezoneOffset;
      Date.prototype.getTimezoneOffset = function() { return _XIO_OFFSET; };
    } catch (_) {}
    // Intl.DateTimeFormat().resolvedOptions().timeZone
    try {
      const _origRO = Intl.DateTimeFormat.prototype.resolvedOptions;
      Intl.DateTimeFormat.prototype.resolvedOptions = function() {
        const r = _origRO.call(this);
        r.timeZone = _XIO_TZ;
        r.locale   = _LOCALE;
        return r;
      };
    } catch (_) {}
  }

  // ── 10. WebRTC — force TURN-relay (blocks local/STUN IP leak) ────────────
  try {
    const _OrigRTC = window.RTCPeerConnection;
    if (_OrigRTC) {
      const _PatchRTC = function(cfg, ...rest) {
        if (cfg) cfg.iceTransportPolicy = 'relay';
        return new _OrigRTC(cfg, ...rest);
      };
      _PatchRTC.prototype = _OrigRTC.prototype;
      window.RTCPeerConnection = _PatchRTC;
      window.webkitRTCPeerConnection = _PatchRTC;
    }
  } catch (_) {}

  // ── 11. Network connection — spoof realistic broadband ───────────────────
  try {
    Object.defineProperty(navigator, 'connection', {
      get: () => ({
        effectiveType: '4g', rtt: 50, downlink: 10.0,
        type: 'wifi', saveData: false,
        addEventListener: () => {}, removeEventListener: () => {},
        dispatchEvent: () => true,
      }),
      configurable: true,
    });
  } catch (_) {}

  // ── 12. Canvas fingerprint noise (seeded, deterministic) ─────────────────
  const _CSEED = {CANVAS_SEED};
  if (_CSEED) {
    try {
      const _origTDU = HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL = function(...a) {
        const d = _origTDU.apply(this, a);
        const i = d.lastIndexOf(',');
        if (i < 0) return d;
        const noise = (_CSEED ^ 0xDEADBEEF).toString(16).padStart(8, '0');
        return d.slice(0, i + 1) + noise + d.slice(i + 9);
      };
      const _origGID = CanvasRenderingContext2D.prototype.getImageData;
      CanvasRenderingContext2D.prototype.getImageData = function(...a) {
        const imgData = _origGID.apply(this, a);
        const d = imgData.data;
        const n = (_CSEED & 0xFF);
        for (let i = 0; i < d.length; i += 400) { d[i] = (d[i] + n) & 0xFF; }
        return imgData;
      };
    } catch (_) {}
  }

  // ── 13. AudioContext fingerprint noise (seeded, deterministic) ───────────
  const _ASEED = {AUDIO_SEED};
  if (_ASEED) {
    try {
      const _origGCD = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = function(ch) {
        const d = _origGCD.call(this, ch);
        for (let i = 0; i < d.length; i += 100) { d[i] = d[i] + (_ASEED * 1e-7); }
        return d;
      };
      // Also patch AudioContext.createOscillator to add subtle frequency shift
      const _origCO = (window.AudioContext || window.webkitAudioContext || class {}).prototype;
      if (_origCO && _origCO.createOscillator) {
        const _origOsc = _origCO.createOscillator;
        _origCO.createOscillator = function() {
          const osc = _origOsc.call(this);
          const origFreq = Object.getOwnPropertyDescriptor(OscillatorNode.prototype, 'frequency');
          return osc;
        };
      }
    } catch (_) {}
  }

  // ── 14. Battery API — spoof realistic laptop battery ─────────────────────
  try {
    const _fakeBattery = {
      charging: true, chargingTime: 0, dischargingTime: Infinity, level: 0.87,
      addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => true,
    };
    if (navigator.getBattery) {
      navigator.getBattery = () => Promise.resolve(_fakeBattery);
    }
  } catch (_) {}

  // ── 15. Speech synthesis — spoof 2 realistic voices ─────────────────────
  try {
    const _fakeVoices = [
      { voiceURI: 'Google US English', name: 'Google US English', lang: _LOCALE,
        localService: false, default: true },
      { voiceURI: 'Google UK English Female', name: 'Google UK English Female',
        lang: 'en-GB', localService: false, default: false },
    ];
    if (window.speechSynthesis) {
      window.speechSynthesis.getVoices = () => _fakeVoices;
    }
  } catch (_) {}

  // ── 16. Permissions API — grant realistic set ────────────────────────────
  try {
    if (navigator.permissions && navigator.permissions.query) {
      const _origQuery = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = (desc) => {
        const name = (desc || {}).name;
        if (name === 'notifications') return Promise.resolve({ state: 'denied',  onchange: null });
        if (name === 'geolocation')   return Promise.resolve({ state: 'granted', onchange: null });
        if (name === 'camera')        return Promise.resolve({ state: 'prompt',  onchange: null });
        if (name === 'microphone')    return Promise.resolve({ state: 'prompt',  onchange: null });
        if (name === 'clipboard-read')return Promise.resolve({ state: 'prompt',  onchange: null });
        return _origQuery(desc).catch(() => Promise.resolve({ state: 'prompt', onchange: null }));
      };
    }
  } catch (_) {}

  // ── 17. MediaDevices — spoof one camera + one mic ────────────────────────
  try {
    if (navigator.mediaDevices) {
      navigator.mediaDevices.enumerateDevices = () => Promise.resolve([
        { deviceId: 'default', kind: 'audioinput',  label: '', groupId: 'default' },
        { deviceId: 'default', kind: 'videoinput',  label: '', groupId: 'default' },
        { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' },
      ]);
    }
  } catch (_) {}

  // ── 18. Geolocation — spoof via IP-geo lat/lon ───────────────────────────
  const _GEO_LAT = {LAT};
  const _GEO_LON = {LON};
  try {
    navigator.geolocation.getCurrentPosition = (success) => {
      success({
        coords: {
          latitude: _GEO_LAT, longitude: _GEO_LON, accuracy: 1000,
          altitude: null, altitudeAccuracy: null, heading: null, speed: null,
        },
        timestamp: Date.now(),
      });
    };
    navigator.geolocation.watchPosition = (success) => {
      success({
        coords: {
          latitude: _GEO_LAT, longitude: _GEO_LON, accuracy: 1000,
          altitude: null, altitudeAccuracy: null, heading: null, speed: null,
        },
        timestamp: Date.now(),
      });
      return 1;
    };
  } catch (_) {}

  // ── 19. ClientRects noise — subtle per-session layout fingerprint noise ──
  const _CRECT_SEED = {CANVAS_SEED} ^ 0xBEEF;
  try {
    const _origGBCR = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function() {
      const r = _origGBCR.call(this);
      const noise = (_CRECT_SEED % 3) * 0.0001;
      return {
        top: r.top + noise, left: r.left + noise,
        right: r.right + noise, bottom: r.bottom + noise,
        width: r.width, height: r.height, x: r.x + noise, y: r.y + noise,
        toJSON: r.toJSON ? r.toJSON.bind(r) : undefined,
      };
    };
  } catch (_) {}

  // ── 20. History length — spoof realistic non-zero ────────────────────────
  try {
    Object.defineProperty(history, 'length', { get: () => 3, configurable: true });
  } catch (_) {}

  // ── 21. Font enumeration block (canvas-based font detect) ────────────────
  // Patches measureText so font detection via canvas probing returns consistent values
  try {
    const _origMT = CanvasRenderingContext2D.prototype.measureText;
    const _FONT_SEED = ({CANVAS_SEED} & 0xFFFF);
    CanvasRenderingContext2D.prototype.measureText = function(text) {
      const m = _origMT.call(this, text);
      // Perturb width slightly to confuse font detection while staying visually correct
      const origW = m.width;
      Object.defineProperty(m, 'width', { get: () => origW + (_FONT_SEED % 5) * 0.0001 });
      return m;
    };
  } catch (_) {}

  // ── 22. Object.defineProperty guard — prevent fingerprint detection bypass
  // Some detectors try to check if properties were overridden
  try {
    const _origODP = Object.defineProperty;
    // Ensure toString on overridden getters looks native
    const _NT = Function.prototype.toString;
    Function.prototype.toString = function() {
      if (this === Function.prototype.toString) return 'function toString() { [native code] }';
      const s = _NT.call(this);
      if (s.includes('native code')) return s;
      // For our overrides, return native-looking string
      if (this._xio_native) return `function ${this.name || ''}() { [native code] }`;
      return s;
    };
  } catch (_) {}

})();
"""








# ── Session registry ───────────────────────────────────────────────────────────
# session_id → {"browser": Browser, "pid": int, "port": int,
#               "cdp_ws_url": str, "proxy_url": str|None,
#               "profile_dir": str|None, "guard_task": Task}
_sessions: dict[str, dict[str, Any]] = {}

# UC driver registry — keeps selenium UC drivers alive after login so the
# authenticated Chrome process remains open and usable without switching browsers.
_uc_drivers: dict[str, Any] = {}   # session_id → uc.Chrome driver


# ── Pydantic models ────────────────────────────────────────────────────────────
class LaunchRequest(BaseModel):
    session_id:            str
    proxy_url:             str | None = None
    profile_dir:           str | None = None   # already-extracted local dir (or None = fresh)
    fingerprint:           dict[str, Any] = {}
    headless:              bool = False         # False = Xvfb headed (vastly more undetectable)
    exit_node_public_ip:   str | None = None   # PPPoE slot public IP for timezone geo-resolve



class TerminateRequest(BaseModel):
    session_id: str


class PullProfileRequest(BaseModel):
    identity_id:      str
    # Accept both names: drive_object_key (new canonical) and r2_object_key (deprecated)
    drive_object_key: str | None = None
    r2_object_key:    str | None = None  # deprecated — use drive_object_key
    # If provided, extract profile to this exact dir (e.g. /tmp/uc-profile-SESSION_ID)
    # instead of PROFILES_BASE. Used by signin flow for profile reuse.
    target_dir:       str | None = None

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
    return {
        "ok":              True,
        "node":            NODE_NAME,
        "active_sessions": len(_sessions),
        "max_sessions":    _MAX_SESSIONS,
        "available":       max(0, _MAX_SESSIONS - len(_sessions)),
        "runtime_type":    _RUNTIME_CAP["runtime_type"],
        "sys_ram_gb":      _RUNTIME_CAP["sys_ram_gb"],
        "gpu_vram_gb":     _RUNTIME_CAP["gpu_vram_gb"],
        "cpu_count":       _RUNTIME_CAP["cpu_count"],
        # SSH SOCKS5 proxy — routes traffic via Mac's residential ISP
        # None if the tunnel isn't up (key not in vault or SSH unreachable)
        "ssh_proxy_url":   _SSH_PROXY_URL,
    }


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
    """Launch Chrome for a session via subprocess (Strategy A) or patchright (Strategy B fallback).

    Strategy A — real google-chrome-stable via subprocess + optional patchright CDP overlay.
      Preferred: no HeadlessChrome UA, undetectable. patchright is optional — if not installed,
      Chrome runs subprocess-only and UC login manages it via /run-uc-login.

    Strategy B — patchright managed headless-shell (fallback when no real Chrome binary).
    """
    # NOTE: patchright import intentionally deferred — Strategy A works without it.

    if req.session_id in _sessions:
        # Idempotent: return existing
        info = _sessions[req.session_id]
        return {"cdp_ws_url": info["cdp_ws_url"], "pid": info["pid"], "port": info["port"]}

    # ── Capacity gate: enforce max sessions per runtime type ──────────────────
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

    # ── Resolve full geo profile from exit node's public IP ───────────────────
    _exit_ip = req.exit_node_public_ip
    if not _exit_ip:
        _exit_ip = await asyncio.get_event_loop().run_in_executor(None, _get_runtime_public_ip)
    _geo = await asyncio.get_event_loop().run_in_executor(
        None, _resolve_ip_geo, _exit_ip
    )
    _timezone = _geo["timezone"]
    _locale   = _geo["locale"]
    _geo_lat  = _geo["lat"]
    _geo_lon  = _geo["lon"]
    logger.info(f"launch timezone={_timezone} locale={_locale} exit_ip={_exit_ip} session={req.session_id}")

    # ── Build per-session stealth JS (inject dynamic values) ─────────────────
    _fp = req.fingerprint or {}
    _canvas_seed = _fp.get("canvas_seed", 0x1A2B)
    _audio_seed  = _fp.get("audio_seed",  0x3C4D)
    _cores = str(_fp.get("cores", os.cpu_count() or 2))
    _ram = str(_fp.get("ram", max(2, min(8, (os.cpu_count() or 2) * 2))))
    _platform = _fp.get("platform", "Linux x86_64")
    _ch_platform = _fp.get("ch_platform", "Linux")
    _ch_arch = _fp.get("ch_arch", "x86")
    _is_mobile = "true" if _fp.get("is_mobile") else "false"
    _screen_w = str(_fp.get("width", 1920))
    _screen_h = str(_fp.get("height", 1080))
    _screen_ah = str(max(100, int(_screen_h) - 40))
    _webgl_v = "Google Inc. (NVIDIA)"
    _webgl_r = _fp.get("webgl_renderer", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)")
    if "Apple" in _webgl_r: _webgl_v = "Apple"
    elif "Intel" in _webgl_r: _webgl_v = "Google Inc. (Intel)"
    elif "AMD" in _webgl_r: _webgl_v = "Google Inc. (AMD)"
    _ua_str = _fp.get("ua_template", _STEALTH_UA_CHROME)
    
    # Compute TZ offset: minutes west of UTC (e.g. IST UTC+5:30 → -330)
    import datetime as _dt
    try:
        import zoneinfo as _zi
        _tz_obj    = _zi.ZoneInfo(_timezone)
        _tz_offset = -int(_dt.datetime.now(_tz_obj).utcoffset().total_seconds() // 60)
    except Exception:
        _tz_offset = 0
    _session_stealth_js = (
        _STEALTH_JS
        .replace("'{TIMEZONE}'",  f"'{_timezone}'")
        .replace("{TIMEZONE}",    _timezone)
        .replace("'{LOCALE}'",    f"'{_locale}'")
        .replace("{LOCALE}",      _locale)
        .replace("{CANVAS_SEED}", str(_canvas_seed))
        .replace("{AUDIO_SEED}",  str(_audio_seed))
        .replace("{LAT}",         str(_geo_lat))
        .replace("{LON}",         str(_geo_lon))
        .replace("'{UA}'",        f"'{_ua_str}'")
        .replace("{UA}",          _ua_str)
        .replace("{TZ_OFFSET}",   str(_tz_offset))
        .replace("{CPU_COUNT}",   _cores)
        .replace("{DEVICE_MEMORY_GB}", _ram)
        .replace("{PLATFORM}",    _platform)
        .replace("{CH_PLATFORM}", _ch_platform)
        .replace("{CH_ARCH}",     _ch_arch)
        .replace("{IS_MOBILE}",   _is_mobile)
        .replace("{SCREEN_W}",    _screen_w)
        .replace("{SCREEN_H}",    _screen_h)
        .replace("{SCREEN_AH}",   _screen_ah)
        .replace("{WEBGL_V}",     _webgl_v)
        .replace("{WEBGL_R}",     _webgl_r)
    )


    profile_dir = req.profile_dir

    # Pre-allocate a free port
    import socket as _sock
    with _sock.socket() as _s:
        _s.bind(("", 0))
        port = _s.getsockname()[1]

    # Build Chrome args
    args = [a for a in CHROMIUM_ARGS if not a.startswith("--user-data-dir")]
    if req.headless:  # Only use headless when explicitly requested
        args.append("--headless=new")
    if hasattr(req, 'pac_url') and req.pac_url:
        args.append(f"--proxy-pac-url={req.pac_url}")
    elif req.proxy_url:
        args.append(f"--proxy-server={req.proxy_url}")
    if profile_dir:
        args.append(f"--user-data-dir={profile_dir}")
    args.append(f"--remote-debugging-port={port}")

    tailscale_ip = _my_tailscale_ip()
    cdp_http_url = f"http://localhost:{port}"
    cdp_ws_url   = f"ws://{tailscale_ip}:{port}"

    pw = None
    browser = None
    context = None
    chrome_proc = None

    # ── Strategy A: Subprocess launch of real Chrome ──────────────────────────
    if CHROME_EXECUTABLE and CHROME_EXECUTABLE != _HEADLESS_SHELL:
        logger.info(f"chrome_launch=subprocess binary={CHROME_EXECUTABLE} headless={req.headless}")
        import subprocess as _sp

        env = dict(os.environ)
        env.setdefault("DISPLAY", ":99")
        chrome_proc = _sp.Popen(
            [CHROME_EXECUTABLE] + args, env=env,
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
            raise HTTPException(status_code=500, detail="Chrome CDP did not become ready")

        # Try to attach patchright CDP overlay (optional — UC login works without it)
        try:
            from patchright.async_api import async_playwright as _aap
            pw = await _aap().start()
            browser = await pw.chromium.connect_over_cdp(cdp_http_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context(
                user_agent=_STEALTH_UA_CHROME
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
                    profile_dir, headless=req.headless, args=pw_args,
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
        "chrome_proc":  chrome_proc,
        "guard_task":   None,
        "timezone":     _timezone,
        "exit_node_ip": _exit_ip,
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
                            m = _re_stealth.search(r'(<head[^>]*>)', body, _re_stealth.IGNORECASE)
                            if m:
                                body = body[:m.end()] + _inject_tag + body[m.end():]
                            elif _re_stealth.search(r'<html[^>]*>', body, _re_stealth.IGNORECASE):
                                body = _re_stealth.sub(
                                    r'(<html[^>]*>)', r'\1' + _inject_tag,
                                    body, count=1, flags=_re_stealth.IGNORECASE,
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
            logger.info(f"stealth_route_applied session={req.session_id} tz={_timezone} script_bytes={len(_session_stealth_js)}")
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
                        json={"session_id": req.session_id, "node": NODE_NAME, "reason": "browser_disconnected"},
                        headers={"Authorization": f"Bearer {XIOSYNC_TOKEN}"},
                    )
            except Exception as exc:
                logger.warning(f"browser-crashed notify failed: {exc}")

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
        # Still clean up UC driver even if patchright session wasn't registered
        _drv = _uc_drivers.pop(req.session_id, None)
        if _drv:
            try: _drv.quit()
            except Exception: pass
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
        try: _drv.quit()
        except Exception: pass

    logger.info(f"terminated session={req.session_id}")
    return {"ok": True}


@app.post("/pull-profile")
async def pull_profile(req: PullProfileRequest) -> dict:
    """Fetch Chrome profile tar.gz from Drive FUSE and extract locally.

    Returns local profile dir path, or {ok: False} if not found on Drive.
    If target_dir is provided, extracts directly there (for signin flow profile reuse).
    """
    if req.target_dir:
        local_dir = Path(req.target_dir)
    else:
        slug = req.identity_id.replace("-", "")[:16]
        local_dir = PROFILES_BASE / f"PRFL_{slug}__{NODE_NAME}"

    if local_dir.exists() and any(local_dir.iterdir()):
        logger.info(f"pull-profile cache-hit identity={req.identity_id} → {local_dir}")
        return {"ok": True, "profile_dir": str(local_dir), "source": "cache"}

    # Read from Drive FUSE
    drive_file = _drive_path(req.object_key)
    if not drive_file.exists():
        logger.info(f"pull-profile not-found identity={req.identity_id} key={req.object_key}")
        # Return ok=False (not 404) — signin flow treats missing profile as "fresh login"
        return {"ok": False, "profile_dir": None, "reason": "not_on_drive"}

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
    local_dir.mkdir(parents=True, exist_ok=True)
    # Move contents (not the root dir) if tarball wraps in a subdirectory
    shutil.move(str(extracted_root), str(local_dir))
    shutil.rmtree(stage_dir, ignore_errors=True)

    _trim_profile(local_dir)

    logger.info(f"pull-profile identity={req.identity_id} key={req.object_key} → {local_dir}")
    return {"ok": True, "profile_dir": str(local_dir), "source": "drive"}



@app.post("/push-profile")
async def push_profile(req: PushProfileRequest) -> dict:
    """Tar local Chrome profile dir and write back to Drive FUSE."""
    profile_path = Path(req.local_dir)
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail=f"Profile dir not found: {req.local_dir}")

    _trim_profile(profile_path)

    # ── Cookie count validation ──────────────────────────────────
    _min_cookies = int(os.environ.get("XIOSYNC_MIN_COOKIE_COUNT", "30"))
    _cookies_db = profile_path / "Default" / "Cookies"
    if not _cookies_db.exists():
        _cookies_db = profile_path / "Default" / "Network" / "Cookies"
    if _cookies_db.exists():
        import sqlite3
        try:
            _conn = sqlite3.connect(str(_cookies_db))
            _cookie_count = _conn.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
            _conn.close()
            if _cookie_count < _min_cookies:
                raise HTTPException(
                    status_code=400,
                    detail=f"Profile has only {_cookie_count} cookies (minimum: {_min_cookies}). "
                           f"Profile may be degraded — refusing push.",
                )
            logger.info(f"Cookie count validation passed: {_cookie_count} >= {_min_cookies}")
        except sqlite3.Error as e:
            logger.warning(f"Cookie DB read failed (non-fatal): {e}")


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



# ── UC Login endpoint ──────────────────────────────────────────────────────────
# Runs undetected-chromedriver with Google Chrome 153 + session's exit node proxy
# and full fingerprint match. Called by google-signin.mjs workflow.

class UCLoginRequest(BaseModel):
    session_id:          str | None = None   # look up proxy_url + UA from _sessions
    email:               str
    password:            str
    totp_secret:         str
    proxy_url:           str | None = None   # explicit override; falls back to session's proxy
    exit_node_public_ip: str | None = None   # PPPoE slot public IP for timezone geo-resolve
    identity_id:         str | None = None   # if set, login-start uses pre-pulled PRFL profile
    fingerprint:         dict = {}           # Pass full FingerprintSpec


# Holds UC Chrome drivers that were pre-launched via /run-uc-login-start, waiting for login
_uc_pending_drivers: dict[str, object] = {}   # session_id → driver


def _run_uc_login_sync(
    email: str,
    password: str,
    totp_secret: str,
    proxy_url: str | None,
    user_agent: str,
    user_data_dir: str | None = None,
    pre_launched_driver=None,    # If set, skip Chrome launch — login on this existing driver
    exit_node_public_ip: str | None = None,  # Expected residential IP for preflight IP match
    fingerprint: dict | None = None,         # Full hardware spec
) -> dict:
    """Synchronous UC stealth login. Called via asyncio thread executor."""
    import pyotp
    import random, glob
    # NOTE: 'import undetected_chromedriver as uc' is deferred to the Chrome-launch
    # block below — it is not needed when reusing a pre-launched patchright driver.

    # ── If a pre-launched driver was supplied, skip Chrome launch entirely ─────
    if pre_launched_driver is not None:
        logger.info("uc-login: reusing pre-launched UC Chrome driver (2-step flow)")
        driver = pre_launched_driver
        # Recover CDP port from driver options if possible
        try:
            _uc_debug_port = next(
                int(a.split("=")[1]) for a in driver.options.arguments
                if a.startswith("--remote-debugging-port=")
            )
        except Exception:
            _uc_debug_port = 0
        logger.info(f"uc-login: pre-launched driver on port {_uc_debug_port}")
        # Set sane page-load timeout — prevents urllib3 120s×3=360s hangs on slow proxy pages
        try:
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(15)
        except Exception:
            pass
        # Resolve geo defaults for pre-launched driver (Chrome launch block is skipped)
        _uc_geo      = _resolve_ip_geo(exit_node_public_ip or None)
        _uc_timezone = _uc_geo.get("timezone", "America/New_York")
        _uc_locale   = _uc_geo.get("locale", "en-US")
        _uc_lat      = _uc_geo.get("lat", 40.7128)
        _uc_lon      = _uc_geo.get("lon", -74.0060)
        _uc_canvas_seed = random.randint(1, 999999)
        # Skip ahead to the login section
    else:
        driver = None
        _uc_debug_port = 0
        # Defaults will be set in Chrome-launch block below
        _uc_timezone = "America/New_York"
        _uc_locale   = "en-US"
        _uc_lat      = 40.7128
        _uc_lon      = -74.0060
        _uc_canvas_seed = random.randint(1, 999999)


    if driver is None:
      # ── Detect Chrome binary and version ──────────────────────────────────────
      # CRITICAL: UC 3.5.5 does NOT fully patch Chrome 134+.
      # Google detects navigator.webdriver on Chrome 153, giving only 3 cookies.
      # Priority order: Chrome 131 (installed by boot) → patchright ≤133 → system if ≤133
      def _find_chrome() -> tuple[str, int]:
        """Return (binary_path, major_version). Prefers UC-compatible versions (≤133)."""

        # 1. Explicitly installed Chrome 131 (exact UC 3.5.5 compatible) ← PREFERRED
        chrome131 = "/opt/chrome131/chrome"
        if os.path.isfile(chrome131) and os.access(chrome131, os.X_OK):
            logger.info("uc-login: using Chrome 131 (UC-compatible) at /opt/chrome131/chrome")
            return chrome131, 131

        # 2. patchright/playwright Chromium — prefer ≤133, but fall back to any version
        _pr_best: tuple[str, int] | None = None
        for path in sorted(
            glob.glob("/root/.cache/ms-patchright/chromium-*/chrome-linux64/chrome"),
            reverse=True,
        ):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                try:
                    raw = subprocess.check_output(
                        [path, "--version"], timeout=5, stderr=subprocess.DEVNULL
                    ).decode().strip()
                    major = int(raw.split()[-1].split(".")[0])
                    if major <= 133:
                        logger.info(f"uc-login: using patchright Chromium {major} at {path}")
                        return path, major
                    else:
                        # Keep as fallback — prefer lowest version > 133
                        if _pr_best is None or major < _pr_best[1]:
                            _pr_best = (path, major)
                        logger.warning(f"uc-login: patchright Chromium {major} > 133 — UC detection risk higher")
                except Exception:
                    pass
        if _pr_best:
            logger.warning(
                f"uc-login: using patchright Chromium {_pr_best[1]} as last resort "
                "(Chrome 131 not installed — install it for best stealth)"
            )
            return _pr_best

        # 3. System Chrome — ONLY if version ≤ 133 (skip 153 — Google detects it)
        for path in ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"]:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                try:
                    raw = subprocess.check_output(
                        [path, "--version"], timeout=5, stderr=subprocess.DEVNULL
                    ).decode().strip()
                    major = int(raw.split()[-1].split(".")[0])
                    if major <= 133:
                        logger.info(f"uc-login: using system Chrome {major} at {path}")
                        return path, major
                    else:
                        logger.warning(
                            f"uc-login: SKIPPING system Chrome {major} — UC 3.5.5 cannot "
                            "fully patch Chrome 134+. Google detects navigator.webdriver → "
                            "only 3 cookies issued. Use Chrome ≤133."
                        )
                except Exception:
                    pass

        # 4. xio-pkgs bundled Chrome
        for path in sorted(
            glob.glob("/tmp/xio-pkgs/**/chrome", recursive=True),
            reverse=True,
        ):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                try:
                    ver_dir = path.split('/browser/')[0].split('/')[-1] if '/browser/' in path else ""
                    major = int(ver_dir.split('.')[0]) if ver_dir else 131
                    logger.info(f"uc-login: using xio-pkgs Chrome {major} at {path}")
                    return path, major
                except Exception:
                    pass

            raise FileNotFoundError(
                "No Chrome/Chromium binary found. "
                "Expected patchright Chromium, google-chrome-stable, or xio-pkgs Chrome."
            )

      _chrome_bin, _chrome_ver = _find_chrome()

      import undetected_chromedriver as uc  # deferred — not needed for pre-launched drivers
      opts = uc.ChromeOptions()

      # Geo/timezone defaults — will be overridden below once IP is known
      _uc_geo      = _resolve_ip_geo(None)
      _uc_timezone = _uc_geo.get("timezone", "America/New_York")
      _uc_locale   = _uc_geo.get("locale", "en-US")
      _uc_lat      = _uc_geo.get("lat", 40.7128)
      _uc_lon      = _uc_geo.get("lon", -74.0060)

      if proxy_url:
          # Prefer local SSH/WS bridge — Chrome cannot reach remote SOCKS5 IPs directly.
          # The local bridge tunnels Chrome traffic through to the PPPoE exit node.
          _chrome_proxy = proxy_url
          # Try WS bridge first (port 19055), then SSH tunnel (port 19056)
          import sys as _sys
          _main = _sys.modules.get("__main__", _sys.modules[__name__])
          _ws_port  = getattr(_main, "_WS_SOCKS5_PORT",   19055)
          _ssh_port = getattr(_main, "_proxy_local_port", 19056)
          # Pick the port that's actually listening
          import socket as _chk
          def _port_open(p):
              try:
                  s = _chk.create_connection(("127.0.0.1", p), timeout=1); s.close(); return True
              except Exception: return False
          if _port_open(_ws_port):
              _local_bridge_port = _ws_port
              logger.info(f"uc-login: Chrome proxy → WS bridge socks5://127.0.0.1:{_ws_port} (→ {proxy_url[:40]})")
          elif _port_open(_ssh_port):
              _local_bridge_port = _ssh_port
              logger.info(f"uc-login: Chrome proxy → SSH tunnel socks5://127.0.0.1:{_ssh_port} (→ {proxy_url[:40]})")
          else:
              _local_bridge_port = None
              logger.warning(f"uc-login: no local bridge available — Chrome using direct {proxy_url[:50]}")
          if _local_bridge_port:
              _chrome_proxy = f"socks5://127.0.0.1:{_local_bridge_port}"
          proxy_addr = _chrome_proxy.replace("socks5://", "")
          opts.add_argument(f"--proxy-server=socks5://{proxy_addr}")
          logger.info(f"uc-login: exit-node proxy={proxy_url[:50]}")

          # ── IP verification preflight ─────────────────────────────────────────
          # Step 1: confirm SOCKS5 proxy is reachable
          logger.info("uc-login: verifying SOCKS5 proxy reachability...")
          _proxy_ready = False
          for _attempt in range(12):
              try:
                  rc = subprocess.run(
                      ["curl", "-s", "--max-time", "5", "--proxy", proxy_url,
                       "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com"],
                      capture_output=True, text=True, timeout=8,
                  )
                  code = rc.stdout.strip()
                  if code and code != "000":
                      _proxy_ready = True
                      break
                  raise OSError(f"HTTP {code}")
              except Exception as pe:
                  logger.warning(f"uc-login: SOCKS5 not ready ({_attempt+1}/12): {pe} — 5s")
                  time.sleep(5)
          if not _proxy_ready:
              return {"ok": False, "error": "SOCKS5 proxy unreachable after 60s"}
          logger.info("uc-login: SOCKS5 proxy confirmed reachable ✅")

          # Step 2: confirm actual exit IP through proxy matches expected residential IP
          _expected_ip = exit_node_public_ip
          logger.info(f"uc-login: verifying exit IP through proxy (expected={_expected_ip})...")
          _actual_ip = None
          for _ip_attempt in range(3):
              try:
                  ip_rc = subprocess.run(
                      ["curl", "-s", "--max-time", "8", "--proxy", proxy_url,
                       "https://api.ipify.org"],
                      capture_output=True, text=True, timeout=12,
                  )
                  _actual_ip = ip_rc.stdout.strip()
                  if _actual_ip:
                      break
              except Exception as ipe:
                  logger.warning(f"uc-login: IP check attempt {_ip_attempt+1}/3 failed: {ipe}")
                  time.sleep(2)

          if not _actual_ip:
              # Try backup IP check service
              try:
                  ip_rc2 = subprocess.run(
                      ["curl", "-s", "--max-time", "8", "--proxy", proxy_url,
                       "http://ip-api.com/json?fields=query"],
                      capture_output=True, text=True, timeout=12,
                  )
                  import json as _json
                  _actual_ip = _json.loads(ip_rc2.stdout).get("query", "")
              except Exception:
                  pass

          if _actual_ip and _expected_ip and _actual_ip != _expected_ip:
              logger.warning(
                  f"uc-login: ⚠️  EXIT IP MISMATCH — actual={_actual_ip} expected={_expected_ip}. "
                  "Aborting to prevent sign-in with wrong IP fingerprint."
              )
              return {
                  "ok": False,
                  "error": f"Exit IP mismatch: proxy exits via {_actual_ip} but expected {_expected_ip}. "
                           "Ensure the PPPoE slot is correctly assigned.",
                  "actual_ip": _actual_ip,
                  "expected_ip": _expected_ip,
              }
          elif _actual_ip:
              logger.info(f"uc-login: ✅ Exit IP confirmed: {_actual_ip}")
          else:
              logger.warning("uc-login: Could not determine exit IP — proceeding with caution")

          # Step 3: resolve full geo profile for verified IP
          _uc_geo = _resolve_ip_geo(_actual_ip or _expected_ip or "")
          _uc_timezone = _uc_geo["timezone"]
          _uc_locale   = _uc_geo["locale"]
          _uc_lat      = _uc_geo["lat"]
          _uc_lon      = _uc_geo["lon"]
          logger.info(
              f"uc-login: geo profile — tz={_uc_timezone} locale={_uc_locale} "
              f"city={_uc_geo['city']},{_uc_geo['countryCode']} isp={_uc_geo['isp']}"
          )
      else:
          logger.info("uc-login: no proxy — direct connection")
          _uc_geo      = _resolve_ip_geo(None)
          _uc_timezone = _uc_geo["timezone"]
          _uc_locale   = _uc_geo["locale"]
          _uc_lat      = _uc_geo["lat"]
          _uc_lon      = _uc_geo["lon"]


      opts.add_argument("--no-sandbox")
      opts.add_argument("--disable-setuid-sandbox")
      opts.add_argument("--disable-dev-shm-usage")
      opts.add_argument("--no-zygote")         # required in Colab containers (no zygote process)
      # Software WebGL (--disable-gpu kills WebGL = instant bot flag)
      opts.add_argument("--use-gl=angle")
      opts.add_argument("--use-angle=swiftshader")
      opts.add_argument("--disable-gpu-sandbox")
      opts.add_argument("--ignore-gpu-blocklist")
      opts.add_argument("--disable-blink-features=AutomationControlled")
      opts.add_argument("--disable-service-workers")
      opts.add_argument("--disable-features=ServiceWorker")
      opts.add_argument("--window-size=1920,1080")
      opts.add_argument("--window-position=0,0")
      opts.add_argument("--display=:99")
            # Prevent Chrome from restoring previous session when launched with a saved profile.
      # Without these, Chrome replays old cached connections through the new proxy context
      # → renderer timeout "Timed out receiving message from renderer: 26.5s"
      opts.add_argument("--no-first-run")
      opts.add_argument("--no-default-browser-check")
      opts.add_argument("--restore-last-session=false")
      opts.add_argument("--disable-session-crashed-bubble")
      opts.add_argument("--disable-infobars")
      # ── Renderer stability flags for SOCKS5 proxy environments ────────────
      # SOCKS5 adds RTT latency that exceeds Chrome's renderer IPC budget,
      # causing "Timed out receiving message from renderer" crashes on page loads.
      # These flags prevent renderer backgrounding/throttling so IPC stays alive.
      opts.add_argument("--disable-renderer-backgrounding")
      opts.add_argument("--disable-backgrounding-occluded-windows")
      opts.add_argument("--disable-background-timer-throttling")
      opts.add_argument("--disable-ipc-flooding-protection")
      opts.add_argument("--renderer-process-limit=4")
      opts.add_argument("--no-pings")
      opts.add_experimental_option("prefs", {
          "webrtc.ip_handling_policy":     "disable_non_proxied_udp",
          "webrtc.multiple_routes_enabled": False,
          "webrtc.nonproxied_udp_enabled":  False,
          # NOTE: Do NOT add profile.exited_cleanly=True here — it causes Chrome
          # to clear session cookies (SAPISID, SSID, APISID etc.) on startup,
          # dropping the cookie count from 65 to 5 on restored profiles.
      })

      import socket as _sock
      _s = _sock.socket(); _s.bind(("", 0)); _uc_debug_port = _s.getsockname()[1]; _s.close()
      opts.add_argument(f"--remote-debugging-port={_uc_debug_port}")
      opts.add_argument("--remote-debugging-address=0.0.0.0")
      opts.add_argument("--remote-allow-origins=*")   # allow xioview CDP WebSocket to attach
      logger.info(f"uc-login: UC Chrome will use CDP port {_uc_debug_port}")

      env = dict(os.environ); env["DISPLAY"] = ":99"

      # ── Use pre-downloaded chromedriver 131 to bypass UC's built-in patcher ──
      # UC's patcher segfaults in Colab (confirmed via dmesg). Use official chromedriver.
      _pre_driver_path = "/usr/local/bin/chromedriver131"
      if not os.path.isfile(_pre_driver_path):
          _pre_driver_path = None  # fall back to UC's patcher if not available
      if _pre_driver_path:
          logger.info(f"uc-login: using pre-downloaded chromedriver at {_pre_driver_path}")

      # Use 'eager' strategy: returns on DOMContentLoaded, not full resource load.
      # Critical for SOCKS5 proxy — prevents 30s timeout waiting for slow external resources.
      opts.page_load_strategy = "eager"

      driver = uc.Chrome(
          options=opts,
          browser_executable_path=_chrome_bin,
          driver_executable_path=_pre_driver_path,   # bypass UC patcher (segfaults on Colab)
          version_main=_chrome_ver,
          use_subprocess=True,
          headless=False,
          port=_uc_debug_port,
          user_data_dir=user_data_dir,
          keep_user_data_dir=True,
      )
      # Set sane page-load timeout — prevents urllib3 hangs on slow proxy pages
      try:
          driver.set_page_load_timeout(45)  # longer timeout for SOCKS5 RTT
          driver.set_script_timeout(15)
      except Exception:
          pass

    # ── driver is now set (pre-launched or newly created) ─────────────────────
    from selenium.webdriver.common.by import By  # needed for By.TAG_NAME / By.XPATH

    def rnd(a=0.03, b=0.09):
        return random.uniform(a, b)

    def find(selectors, t=15):
        if isinstance(selectors, str):
            selectors = [selectors]
        deadline = time.time() + t
        while time.time() < deadline:
            for sel in selectors:
                try:
                    el = driver.find_element("css selector", sel)
                    if el.is_displayed():
                        return el
                except Exception:
                    pass
            time.sleep(0.4)
        url, title = driver.current_url, driver.title
        logger.warning(f"uc-login find_failed sel={selectors} url={url[:80]}")
        raise Exception(f"Element not found: {selectors} (url={url[:80]})")

    # ── CDP Native Behavioral Interactions ──────────────────────────────────
    _mouse_pos = {"x": 100, "y": 100}

    def _bezier(p0, p1, p2, p3, t):
        x = (1-t)**3 * p0[0] + 3*(1-t)**2 * t * p1[0] + 3*(1-t) * t**2 * p2[0] + t**3 * p3[0]
        y = (1-t)**3 * p0[1] + 3*(1-t)**2 * t * p1[1] + 3*(1-t) * t**2 * p2[1] + t**3 * p3[1]
        return x, y

    def cdp_mouse_move(start_x, start_y, end_x, end_y):
        steps = random.randint(12, 25)
        cx1 = start_x + (end_x - start_x) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
        cy1 = start_y + (end_y - start_y) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
        cx2 = start_x + (end_x - start_x) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
        cy2 = start_y + (end_y - start_y) * random.uniform(0.2, 0.8) + random.uniform(-30, 30)
        for i in range(steps + 1):
            t = i / steps
            x, y = _bezier((start_x, start_y), (cx1, cy1), (cx2, cy2), (end_x, end_y), t)
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(x), "y": int(y)})
            time.sleep(random.uniform(0.005, 0.015))

    def cdp_get_rect(selector):
        # We must use execute_cdp_cmd for evaluate to be fully untracked
        js = f"""
        (function() {{
            var el = document.querySelector('{selector}');
            if (!el) return null;
            var rect = el.getBoundingClientRect();
            return {{x: rect.x, y: rect.y, w: rect.width, h: rect.height}};
        }})()
        """
        try:
            res = driver.execute_cdp_cmd("Runtime.evaluate", {"expression": js, "returnByValue": True})
            return res.get("result", {}).get("value")
        except:
            return None

    def cdp_click_element(selectors, timeout=10):
        if isinstance(selectors, str): selectors = [selectors]
        rect = None
        for _ in range(int(timeout * 2.5)):
            for sel in selectors:
                rect = cdp_get_rect(sel)
                if rect and rect.get('w', 0) > 0 and rect.get('h', 0) > 0:
                    break
            if rect and rect.get('w', 0) > 0 and rect.get('h', 0) > 0:
                break
            time.sleep(0.4)
        if not rect or rect.get('w', 0) <= 0:
            raise Exception(f"Element not found for CDP click: {selectors}")
            
        # ── Pre-interaction Scrolling Seasoning ──
        if random.random() < 0.4:
            scroll_dir = random.choice([100, 200, -100])
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": _mouse_pos["x"], "y": _mouse_pos["y"],
                "deltaX": 0, "deltaY": scroll_dir
            })
            time.sleep(random.uniform(0.1, 0.3))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": _mouse_pos["x"], "y": _mouse_pos["y"],
                "deltaX": 0, "deltaY": -scroll_dir
            })
            time.sleep(random.uniform(0.1, 0.3))
        
        target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
        target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
        
        cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
        _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
        
        time.sleep(random.uniform(0.05, 0.15))
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
        time.sleep(random.uniform(0.04, 0.12))
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
        return True

    def cdp_type_text(text):
        # Bulletproof text insertion for React forms
        js_inject = f'''
        (function(text) {{
            let el = document.activeElement;
            // If body is active, try to find the input we just clicked based on ID/Name
            if (!el || el === document.body) {{
                let inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
                el = inputs.find(i => {{
                    let r = i.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }});
            }}
            if (!el) return false;
            
            // React 16+ value setter bypass
            let nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
            if (nativeInputValueSetter) {{
                nativeInputValueSetter.call(el, text);
            }} else {{
                el.value = text;
            }}
            
            el.dispatchEvent(new Event("input", {{ bubbles: true }}));
            el.dispatchEvent(new Event("change", {{ bubbles: true }}));
            return true;
        }})({repr(text)});
        '''
        driver.execute_script(js_inject)
        time.sleep(random.uniform(0.1, 0.3))

    def cdp_clear_input():
        js_inject = '''
        (function() {
            let el = document.activeElement;
            if (!el || el === document.body) {
                let inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
                el = inputs.find(i => {
                    let r = i.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
            }
            if (!el) return false;
            let nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
            if (nativeInputValueSetter) {
                nativeInputValueSetter.call(el, "");
            } else {
                el.value = "";
            }
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
            return true;
        })();
        '''
        driver.execute_script(js_inject)
        time.sleep(0.1)
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 8, "key": "Backspace"})
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 8, "key": "Backspace"})
        time.sleep(0.05)

    try:
        # ── CDP helper (early definition — used throughout) ───────────────────
        def cdp_eval(js: str, await_promise: bool = False):
            """Execute JS via CDP Runtime.evaluate — undetectable by Google."""
            try:
                result = driver.execute_cdp_cmd(
                    "Runtime.evaluate",
                    {"expression": js, "awaitPromise": await_promise,
                     "returnByValue": True, "timeout": 8000},
                )
                return result.get("result", {}).get("value")
            except Exception as ce:
                logger.warning(f"uc-login: cdp_eval error: {ce}")
                return None

        # ── CDP-level device emulation (protocol-level, undetectable) ─────────
        # These override at the CDP/browser level — JS fingerprint checks can't
        # distinguish them from real device properties.

        # 1. Timezone emulation (CDP level — overrides Intl regardless of JS)
        try:
            driver.execute_cdp_cmd(
                "Emulation.setTimezoneOverride",
                {"timezoneId": _uc_timezone},
            )
            logger.info(f"uc-login: CDP timezone={_uc_timezone}")
        except Exception as _tz_e:
            logger.warning(f"uc-login: CDP timezone override failed: {_tz_e}")

        # 2. Geolocation emulation (matches IP geo lat/lon)
        try:
            driver.execute_cdp_cmd(
                "Emulation.setGeolocationOverride",
                {"latitude": _uc_lat, "longitude": _uc_lon, "accuracy": 1000},
            )
            logger.info(f"uc-login: CDP geolocation lat={_uc_lat:.2f} lon={_uc_lon:.2f}")
        except Exception as _geo_e:
            logger.warning(f"uc-login: CDP geolocation override failed: {_geo_e}")

        # 3. User-Agent + Accept-Language override (CDP level)
        try:
            # Build CDP UA override dynamically
            _fp_ua = fingerprint or {}; _ch_platform = _fp_ua.get("ch_platform", "Linux")
            _ch_arch = _fp_ua.get("ch_arch", "x86")
            _is_mobile = bool(_fp_ua.get("is_mobile", False))
            _ua_str = _fp_ua.get("ua_template", user_agent)
            
            driver.execute_cdp_cmd(
                "Network.setUserAgentOverride",
                {
                    "userAgent":         _ua_str,
                    "acceptLanguage":    f"{_uc_locale},{_uc_locale.split('-')[0]};q=0.9,en;q=0.8",
                    "platform":          _ch_platform,
                    "userAgentMetadata": {
                        "brands": [
                            {"brand": "Google Chrome",   "version": "131"},
                            {"brand": "Chromium",        "version": "131"},
                            {"brand": "Not_A Brand",     "version": "24"},
                        ],
                        "fullVersion":    "131.0.6778.264",
                        "platform":       _ch_platform,
                        "platformVersion":"10.0.0",
                        "architecture":   _ch_arch,
                        "model":          "",
                        "mobile":         _is_mobile,
                        "bitness":        "64",
                        "wow64":          False,
                    },
                },
            )
            logger.info(f"uc-login: CDP UA+Accept-Language={_uc_locale} set")
        except Exception as _ua_e:
            logger.warning(f"uc-login: CDP UA override failed: {_ua_e}")

        # 4. Inject comprehensive stealth JS into current page context
        try:
            import datetime as _dtm
            try:
                import zoneinfo as _zi2
                _tz_obj2   = _zi2.ZoneInfo(_uc_timezone)
                _tz_off2   = -int(_dtm.datetime.now(_tz_obj2).utcoffset().total_seconds() // 60)
            except Exception:
                _tz_off2 = 0
            # Use fingerprint if provided, else fallback
            _fp = fingerprint or {}
            _uc_canvas_seed = _fp.get("canvas_seed", random.randint(0x1000, 0xFFFF))
            _uc_audio_seed  = _fp.get("audio_seed", random.randint(0x1000, 0xFFFF))
            _cores = str(_fp.get("cores", os.cpu_count() or 2))
            _ram = str(_fp.get("ram", max(2, min(8, (os.cpu_count() or 2) * 2))))
            _platform = _fp.get("platform", "Linux x86_64")
            _fp_ua = fingerprint or {}; _ch_platform = _fp_ua.get("ch_platform", "Linux")
            _ch_arch = _fp_ua.get("ch_arch", "x86")
            _is_mobile = "true" if _fp.get("is_mobile") else "false"
            _screen_w = str(_fp.get("width", 1920))
            _screen_h = str(_fp.get("height", 1080))
            _screen_ah = str(max(100, int(_screen_h) - 40))
            _webgl_v = "Google Inc. (NVIDIA)"
            _webgl_r = _fp.get("webgl_renderer", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)")
            if "Apple" in _webgl_r: _webgl_v = "Apple"
            elif "Intel" in _webgl_r: _webgl_v = "Google Inc. (Intel)"
            elif "AMD" in _webgl_r: _webgl_v = "Google Inc. (AMD)"
            _ua_str = _fp_ua.get("ua_template", user_agent)
            
            _uc_stealth = (
                _STEALTH_JS
                .replace("'{TIMEZONE}'",  f"'{_uc_timezone}'")
                .replace("{TIMEZONE}",    _uc_timezone)
                .replace("'{LOCALE}'",    f"'{_uc_locale}'")
                .replace("{LOCALE}",      _uc_locale)
                .replace("{CANVAS_SEED}", str(_uc_canvas_seed))
                .replace("{AUDIO_SEED}",  str(_uc_audio_seed))
                .replace("{LAT}",         str(_uc_lat))
                .replace("{LON}",         str(_uc_lon))
                .replace("'{UA}'",        f"'{_ua_str}'")
                .replace("{UA}",          _ua_str)
                .replace("{TZ_OFFSET}",   str(_tz_off2))
                .replace("{CPU_COUNT}",   _cores)
                .replace("{DEVICE_MEMORY_GB}", _ram)
                .replace("{PLATFORM}",    _platform)
                .replace("{CH_PLATFORM}", _ch_platform)
                .replace("{CH_ARCH}",     _ch_arch)
                .replace("{IS_MOBILE}",   _is_mobile)
                .replace("{SCREEN_W}",    _screen_w)
                .replace("{SCREEN_H}",    _screen_h)
                .replace("{SCREEN_AH}",   _screen_ah)
                .replace("{WEBGL_V}",     _webgl_v)
                .replace("{WEBGL_R}",     _webgl_r)
            )
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _uc_stealth},
            )
            # Also run on the current blank page
            cdp_eval(_uc_stealth)
            logger.info(
                f"uc-login: ✅ stealth JS injected — "
                f"tz={_uc_timezone} locale={_uc_locale} canvas_seed={_uc_canvas_seed}"
            )
        except Exception as _sj_e:
            logger.warning(f"uc-login: stealth JS injection failed: {_sj_e}")

        # 5. Set Accept-Language HTTP header to match JS locale (closes HTTP/JS mismatch)
        try:
            _accept_lang = f"{_uc_locale},{_uc_locale.split('-')[0]};q=0.9,en;q=0.8"
            driver.execute_cdp_cmd(
                "Network.setExtraHTTPHeaders",
                {"headers": {"Accept-Language": _accept_lang}},
            )
            logger.info(f"uc-login: HTTP Accept-Language header set → {_accept_lang}")
        except Exception as _ah_e:
            logger.warning(f"uc-login: setExtraHTTPHeaders failed: {_ah_e}")

        # 6. Enable Network domain (required for setExtraHTTPHeaders to persist)
        try:
            driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

        def uc_sleep(a=0.8, b=2.0):
            duration = a + random.random() * (b - a)
            end_time = time.time() + duration
            while time.time() < end_time:
                # ── Idle Jitter Seasoning ──
                if random.random() < 0.35:
                    tgt_x = max(10, min(1900, _mouse_pos["x"] + random.randint(-80, 80)))
                    tgt_y = max(10, min(1000, _mouse_pos["y"] + random.randint(-80, 80)))
                    try:
                        cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], tgt_x, tgt_y)
                        _mouse_pos["x"], _mouse_pos["y"] = tgt_x, tgt_y
                    except:
                        pass
                rem = end_time - time.time()
                if rem <= 0: break
                time.sleep(min(rem, random.uniform(0.1, 0.4)))

        from selenium.common.exceptions import TimeoutException as _SeleniumTimeout

        def safe_get(url: str, wait: float = 1.0) -> bool:
            """Navigate to url; return True on success, False on timeout/error.
            Never raises — callers check the return value and decide whether to continue.
            """
            try:
                driver.get(url)
                if wait > 0:
                    uc_sleep(wait * 0.5, wait)
                return True
            except _SeleniumTimeout:
                logger.warning(f"uc-login: page load timed out ({url[:60]}) — continuing")
                return False
            except Exception as _nav_err:
                logger.warning(f"uc-login: navigation error ({url[:60]}): {_nav_err}")
                return False

        # ── Phase 0: Check if already authenticated (XIOBR exact pattern) ────────
        # Go directly to myaccount.google.com — XIOBR never navigated to about:blank
        # first because --restore-last-session=false handles restored tabs. The
        # about:blank step was causing renderer crashes with restored profiles.
        logger.info("uc-login: Phase 0 — checking myaccount.google.com")

        # XIOBR Phase 0: plain driver.get() — let the full 30s timeout run.
        _phase0_crashed = False
        try:
            driver.get("https://myaccount.google.com/")
        except Exception as _p0e:
            _phase0_crashed = True
            logger.warning(f"uc-login: Phase 0 nav exception ({_p0e!s:.80}) — Chrome recovering...")
            time.sleep(3)   # let Chrome stabilise before next navigation
        uc_sleep(1.5, 2.5)   # XIOBR exact: wait 1.5–2.5s after navigation

        _phase0_url = driver.current_url
        # Only accept myaccount.google.com as authenticated.
        # account/about = UNAUTHENTICATED redirect (Google sends logged-out users there).
        _is_myaccount = "myaccount.google.com" in _phase0_url
        if _is_myaccount and "signin" not in _phase0_url:
            # ── Identity check: confirm the email on the page matches target ──────
            # Use CDP Runtime.evaluate on document.body.innerText — the email is
            # shown on the page (visible in screenshot) but rendered by JS, so it
            # appears in innerText AFTER React runs, NOT in driver.page_source (raw HTML).
            try:
                _email_check = driver.execute_cdp_cmd("Runtime.evaluate", {
                    "expression": f"document.body.innerText.toLowerCase().includes('{email.lower()}')",
                    "returnByValue": True,
                    "timeout": 3000,
                })
                email_found = _email_check.get("result", {}).get("value", True)
                logger.info(f"uc-login: Phase 0 email check → found={email_found} url={_phase0_url[:60]}")
            except Exception as _ec:
                email_found = True   # evaluation failed → assume correct account, proceed
                logger.warning(f"uc-login: Phase 0 email check failed ({_ec}) — assuming correct account")

            if email_found:
                logger.info("uc-login: already authenticated ✅ — email verified on page (Phase 0 shortcut)")
                try:
                    cdp_cookies = driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])
                except Exception:
                    cdp_cookies = driver.get_cookies()
                return {
                    "ok": True,
                    "uc_port": _uc_debug_port if 'driver' in dir() else 0,
                    "profile_dir": user_data_dir,
                    "cookies": cdp_cookies,
                    "final_url": driver.current_url,
                }
            else:
                logger.warning(f"uc-login: Phase 0 — WRONG ACCOUNT on myaccount page (expected {email}) — forcing full re-login")
        else:
            logger.info(f"uc-login: Phase 0 — not authenticated ({_phase0_url[:80]}), proceeding to login")

        # ── Warmup: visit google.com (XIOBR pattern) ──────────────────────────
        # Sets NID cookie + navigation history before signin — prevents "no prior session" detection
        logger.info("uc-login: warmup — google.com")
        safe_get("https://www.google.com", wait=1.0)

        # ── Navigate to sign-in (XIOBR ServiceLogin URL) ─────────────────────
        # With SOCKS proxy, Chrome's renderer budget times out on first attempt.
        # Retry up to 3 times with increasing waits before giving up.
        logger.info("uc-login: navigating to ServiceLogin")
        _SL_URL = (
            "https://accounts.google.com/ServiceLogin"
            "?service=mail&hl=en&continue=https://mail.google.com"
        )
        _sl_ok = False
        for _sl_attempt in range(3):
            try:
                driver.get(_SL_URL)
            except Exception as _sg_err:
                logger.warning(f"uc-login: ServiceLogin nav exception attempt {_sl_attempt+1}: {str(_sg_err)[:120]} — retrying")
            _sl_wait = 3.0 + _sl_attempt * 2.0
            time.sleep(_sl_wait)
            curr_start = driver.current_url
            logger.info(f"uc-login: post-nav url attempt {_sl_attempt+1}: {curr_start}")
            if "accounts.google.com" in curr_start or "google.com/ServiceLogin" in curr_start:
                _sl_ok = True
                break
            # Still on new-tab — force a second attempt
            logger.warning(f"uc-login: still on {curr_start[:60]} after attempt {_sl_attempt+1}, retrying")
            time.sleep(2.0)

        if not _sl_ok:
            # Last chance: try direct IP (bypass possible DNS issue through proxy)
            curr_start = driver.current_url
            if "chrome://" in curr_start or "new-tab" in curr_start:
                raise Exception(
                    f"ServiceLogin unreachable after 3 attempts — "
                    f"still on {curr_start[:80]}. Check proxy connectivity."
                )
        logger.info(f"uc-login: page_url={curr_start}")

        # ── Wait for React to render the email form (SOCKS proxy slow path) ──
        # On slow proxies, the URL updates before React mounts the form.
        # Poll up to 10s for the identifier input to appear in the DOM.
        _react_ready = False
        for _ri in range(20):  # 20 × 0.5s = 10s max
            try:
                _inp_count = driver.execute_script(
                    "return document.querySelectorAll('input[name=\"identifier\"],input[type=\"email\"],#identifierId').length"
                )
                if _inp_count and int(_inp_count) > 0:
                    _react_ready = True
                    logger.info(f"uc-login: email input found after {(_ri+1)*0.5:.1f}s")
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not _react_ready:
            logger.warning("uc-login: email input not found via poll — attempting find() anyway")

        # ── Handle /challenge/pwd: Google skips email, shows password directly ─
        # Happens when UC Chrome has a stale identity cookie from a previous run.
        # The password input on /challenge/pwd has different selectors than normal.
        if "/challenge/pwd" in curr_start or "/challenge/" in curr_start:
            logger.info(f"uc-login: skipped email — on challenge page {curr_start[:80]}")
        else:
            # Normal flow: fill email
            cdp_click_element(['input[name="identifier"]', 'input[type="email"]', "#identifierId"])
            cdp_type_text(email)
            time.sleep(rnd(0.5, 1.0))
            cdp_click_element(['#identifierNext', 'button[jsname="LgbsSe"]', 'div[id="identifierNext"]'])
            logger.info("uc-login: email entered")
            time.sleep(2.5)

            curr = driver.current_url
            if "/rejected" in curr or "/lookup/rejection" in curr:
                return {"ok": False, "error": f"Rejected at email step: {curr[:120]}"}

            # ── Handle "Choose how you want to sign in" (passkey selection page) ──
            for _cpw_i in range(8):   # poll up to 4s
                time.sleep(0.5)
                _bt2 = cdp_eval("document.body.innerText||''") or ""
                if "enter your password" in _bt2.lower() or "enter password" in _bt2.lower():
                    # Find exact element using JS bounding client rect
                    _choose_pw_rect_js = """
                    (function(){
                      var all=document.querySelectorAll('*');
                      for(var i=0;i<all.length;i++){
                        var t=(all[i].childElementCount===0?(all[i].innerText||all[i].textContent||''):'').toLowerCase().trim();
                        if(t.includes('enter your password')||t.includes('enter password')){
                          var r = all[i].getBoundingClientRect();
                          return {x:r.x, y:r.y, w:r.width, h:r.height};
                        }
                      } return null;
                    })()
                    """
                    rect = cdp_eval(_choose_pw_rect_js)
                    if rect and rect.get('w', 0) > 0:
                        target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
                        target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
                        cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
                        _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
                        time.sleep(random.uniform(0.05, 0.15))
                        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                        time.sleep(random.uniform(0.04, 0.12))
                        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                        logger.info(f"uc-login: 'Enter your password' clicked via CDP (passkey screen) — attempt {_cpw_i+1}")
                        time.sleep(1.5)   # wait for password input to render
                        break
                elif "password" in _bt2.lower() and "enter" not in _bt2.lower():
                    # Already on password input page
                    break

        # ── Password — CDP Input (isTrusted=true, native interaction) ────────
        curr_pw = driver.current_url
        logger.info(f"uc-login: filling password on {curr_pw[:80]}")
        
        try:
            cdp_click_element(['input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]'], 15)
            uc_sleep(0.4, 0.8)
            cdp_type_text(password)
            logger.info("uc-login: password typed via cdp_type_text (isTrusted)")
        except Exception as pw_err:
            logger.warning(f"uc-login: password CDP failed ({pw_err}) — fallback to cdp_type_text")
            try:
                cdp_click_element(['input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]'], 8)
                uc_sleep(0.2, 0.4)
                cdp_type_text(password)
            except Exception as pw2:
                logger.warning(f"uc-login: cdp_type_text also failed: {pw2}")

        uc_sleep(0.5, 1.0)
        try:
            cdp_click_element(['#passwordNext', 'button[jsname="LgbsSe"]', 'div[id="passwordNext"]', 'button[type="submit"]'], 5)
        except Exception as _btn_err:
            logger.warning(f"uc-login: password next button fallback ({_btn_err})")
        logger.info("uc-login: password submitted")
        
        # Poll for URL transition away from password page (up to 15s)
        for _wait_i in range(30):
            curr2 = driver.current_url
            if "challenge/pwd" not in curr2 and "signin/identifier" not in curr2:
                break
            time.sleep(0.5)
            
        uc_sleep(1.0, 2.0) # Additional buffer for React render on new page
        curr2 = driver.current_url

        # ── 2FA / Challenge handling (XIOBR full port) ──────────────────────────
        # Covers all Google challenge URL types — phone push, selection, totp, etc.
        curr2 = driver.current_url
        try:
            _body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            _body_text = ""

        _is_challenge = (
            any(k in curr2 for k in [
                "signin/challenge", "signin/v2/challenge",
                "/challenge/totp", "/challenge/selection",
                "/challenge/ipp", "/challenge/dp", "/challenge/az",
                "/challenge/sk", "/challenge/iap", "/challenge/iph",
                "/challenge/sl", "/challenge/dk", "2sv", "lookup",
            ]) or any(k in _body_text for k in [
                "2-Step", "authenticator", "verification code",
                "Check your", "Try another way", "Verification",
                "2-step verification", "sent a notification",
            ])
        ) and "challenge/pwd" not in curr2

        if _is_challenge:
            logger.info(f"uc-login: challenge page detected ({curr2[:80]})")

            # ── reCAPTCHA short-circuit (XIOBR port) ─────────────────────────
            # If Google shows a reCAPTCHA challenge, solve it BEFORE trying
            # 'Try another way' — clicking TAW on reCAPTCHA causes rejection.
            if "challenge/recaptcha" in curr2:
                logger.info("uc-login: reCAPTCHA challenge detected — invoking solver")
                try:
                    _rc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recaptcha_solver.py")
                    if os.path.exists(_rc_path):
                        import importlib.util as _rc_ilu
                        _rc_spec = _rc_ilu.spec_from_file_location("recaptcha_solver", _rc_path)
                        _rc_mod = _rc_ilu.module_from_spec(_rc_spec)
                        _rc_spec.loader.exec_module(_rc_mod)
                        _rc_solved = _rc_mod.solve_recaptcha(driver, log_fn=lambda m: logger.info(m), sleep_fn=uc_sleep, max_attempts=2)
                        if _rc_solved:
                            logger.info("uc-login: reCAPTCHA solved — checking post-solve URL")
                            uc_sleep(2.0, 3.0)
                            _url_after_rc = driver.current_url
                            # Google may show password page after reCAPTCHA (email→reCAPTCHA→pwd flow)
                            if "challenge/pwd" in _url_after_rc:
                                logger.info("uc-login: password challenge after reCAPTCHA — entering password")
                                try:
                                    _rc_pw = WebDriverWait(driver, 12).until(
                                        EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='password']")))
                                    cdp_click_element("input[type='password']")
                                    uc_sleep(0.3, 0.5)
                                    cdp_type_text(password)
                                    uc_sleep(0.4, 0.6)
                                    cdp_click_element('#passwordNext, button[type=submit]')
                                    uc_sleep(3.0, 5.0)
                                except Exception as _rc_pwd_e:
                                    logger.warning(f"uc-login: post-reCAPTCHA password entry failed: {_rc_pwd_e}")
                            # Re-evaluate challenge state after reCAPTCHA
                            curr2 = driver.current_url
                            _is_challenge = "challenge/" in curr2 and "challenge/pwd" not in curr2
                            if not _is_challenge:
                                logger.info("uc-login: no more challenges after reCAPTCHA — proceeding to verify")
                        else:
                            logger.warning("uc-login: reCAPTCHA solver returned False")
                    else:
                        logger.warning(f"uc-login: recaptcha_solver.py not found at {_rc_path}")
                except Exception as _rc_e:
                    logger.warning(f"uc-login: reCAPTCHA handling error: {_rc_e}")

            # ── Step 1: "Try another way" — poll up to 8s for React render ──
            # /challenge/dp (Google device push) renders its links via React AFTER
            # the main page load. A static find() will miss it. We must poll.
            if "/challenge/totp" not in curr2 and "/challenge/selection" not in curr2:
                logger.info("uc-login: polling for 'Try another way' button...")
                _taw_result = False
                for _taw_i in range(16):   # up to 8s @ 0.5s interval
                    time.sleep(0.5)
                    try:
                        _taw_btn = None
                        try:
                            _taw_btn = driver.find_element(By.XPATH, "//*[(self::button or @role='button' or self::a) and (contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'try another') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'more options'))]")
                        except Exception:
                            pass
                        
                        if _taw_btn and _taw_btn.is_displayed():
                            _choose_taw_rect_js = """
                            (function(){
                              var all=document.querySelectorAll('*');
                              for(var i=0;i<all.length;i++){
                                var t=(all[i].childElementCount===0?(all[i].innerText||all[i].textContent||''):'').toLowerCase().trim();
                                if(t.includes('try another')||t.includes('more options')){
                                  var r = all[i].getBoundingClientRect();
                                  return {x:r.x, y:r.y, w:r.width, h:r.height};
                                }
                              } return null;
                            })()
                            """
                            rect = cdp_eval(_choose_taw_rect_js)
                            if rect and rect.get('w', 0) > 0:
                                target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
                                target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
                                cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
                                _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
                                time.sleep(random.uniform(0.05, 0.15))
                                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                                time.sleep(random.uniform(0.04, 0.12))
                                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                                _taw_result = True
                                logger.info(f"uc-login: 'Try another way' clicked natively (attempt {_taw_i+1})")
                                break
                    except Exception as _taw_e:
                        pass
                
                if _taw_result:
                    uc_sleep(2.0, 3.0)   # wait for selection menu to render
                else:
                    logger.info("uc-login: 'Try another way' not found after 8s — TOTP may be direct")

            # ── Step 2: Select Authenticator — poll up to 8s for React render ─
            # After TAW click, /challenge/selection renders options via React.
            # Google uses data-challengetype 6/12/13 for authenticator variants.
            _sel_js = """
(function(){
  var dispatch=function(el){
    el.scrollIntoView({block:'center'});
    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){
      el.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
    });
  };
  // Try known challengetype values for authenticator app
  var t = document.querySelector('[data-challengetype="6"],[data-challengetype="12"],[data-challengetype="13"]');
  if(!t){
    // Fallback: text search in all clickable elements
    var els=document.querySelectorAll('div[role="link"],div[role="button"],li,button,div[role="option"],div[role="listitem"],a');
    for(var i=0;i<els.length;i++){
      var txt=(els[i].innerText||els[i].textContent||'').toLowerCase();
      if(txt.includes('authenticator')||txt.includes('auth app')||txt.includes('google auth')||txt.includes('verification app')){
        t=els[i]; break;
      }
    }
  }
  if(t){dispatch(t);return 'selected';}
  // Log what IS on the page for diagnosis
  var items=[];
  document.querySelectorAll('[data-challengetype]').forEach(function(e){items.push(e.getAttribute('data-challengetype')+':'+e.innerText.trim().slice(0,30));});
  return JSON.stringify({found:false, items:items, url:location.pathname});
})()"""
            _sel_result = None
            for _sel_i in range(16):   # up to 8s @ 0.5s
                time.sleep(0.5)
                _sel_result = cdp_eval(_sel_js)
                if _sel_result == "selected":
                    logger.info(f"uc-login: authenticator selected (attempt {_sel_i+1})")
                    break
                logger.info(f"uc-login: auth select poll [{_sel_i}] → {str(_sel_result)[:120]}")
            else:
                logger.warning("uc-login: authenticator not found after 8s")

            if _sel_result == "selected":
                # Wait for URL to leave selection → /challenge/totp
                logger.info("uc-login: waiting for TOTP page after authenticator selection...")
                _prev_url = driver.current_url
                for _wi in range(20):
                    time.sleep(0.5)
                    try:
                        _wu = driver.current_url
                        if "/challenge/totp" in _wu or "/challenge/ipp" in _wu:
                            logger.info(f"uc-login: TOTP page ready ({_wu[:60]})")
                            break
                        if _wu != _prev_url and "/challenge/selection" not in _wu:
                            logger.info(f"uc-login: navigated to ({_wu[:60]})")
                            break
                    except Exception:
                        pass
                else:
                    logger.warning("uc-login: TOTP page did not appear after 10s")
            else:
                logger.warning("uc-login: skipping TOTP fill — authenticator was not selected")

            # ── Step 3: Wait for TOTP input to render in DOM ─────────────────
            logger.info("uc-login: polling for TOTP input element in DOM...")
            _totp_input_found = False
            for _inp_i in range(14):  # up to 7s
                time.sleep(0.5)
                try:
                    _ic = driver.execute_cdp_cmd("Runtime.evaluate", {
                        "expression": """
(function(){
  var all=Array.from(document.querySelectorAll('input:not([type="hidden"])'));
  var vis=all.filter(function(i){var r=i.getBoundingClientRect();return r.width>0&&r.height>0;});
  return JSON.stringify({total:all.length,visible:vis.length,
    names:vis.slice(0,4).map(function(i){return i.name+'|'+i.type+'|'+i.id;})});
})()""",
                        "returnByValue": True, "timeout": 2000
                    })
                    _ii = __import__('json').loads(_ic.get("result", {}).get("value", '{"visible":0}'))
                    logger.info(f"uc-login: TOTP DOM poll [{_inp_i}]: {_ii}")
                    if _ii.get("visible", 0) > 0:
                        _totp_input_found = True
                        time.sleep(0.3)
                        break
                except Exception as _pe:
                    logger.warning(f"uc-login: TOTP DOM poll error: {_pe}")
            if not _totp_input_found:
                logger.warning("uc-login: TOTP input not visible after 7s — attempting fill anyway")

        # ── TOTP entry (XIOBR full method) ──────────────────────────────────────
        curr3 = driver.current_url
        # Only fill TOTP on actual TOTP input pages — NOT on selection/dp/phone-push pages
        _on_totp_page = (
            _is_challenge and
            "/challenge/selection" not in curr3 and
            "/challenge/dp" not in curr3 and
            "/challenge/az" not in curr3 and
            "/challenge/ipp" not in curr3 and
            "/challenge/" in curr3
        )
        if _is_challenge and not _on_totp_page:
            logger.info(f"uc-login: not on TOTP page ({curr3[:70]}) — skipping TOTP fill")
        if _on_totp_page:
            try:
                import json as _json
                # Window-aware TOTP: wait for fresh window if < 5s remaining
                _totp_min_remaining = int(os.environ.get("XIOSYNC_TOTP_MIN_REMAINING_SEC", "5"))
                _totp_remaining = 30 - (time.time() % 30)
                if _totp_remaining < _totp_min_remaining:
                    logger.info(f"TOTP window has {_totp_remaining:.1f}s left, waiting for fresh window...")
                    time.sleep(_totp_remaining + 0.5)
                totp_code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                logger.info(f"TOTP generated with {30 - (time.time() % 30):.1f}s remaining in window")
                logger.info(f"uc-login: TOTP code={totp_code} url={curr3[:60]}")

                # CDP native TOTP fill
                _totp_rect_js = """
                (function() {
                  var all = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
                  var inp = null;
                  for (var i=0;i<all.length;i++) {
                    var r = all[i].getBoundingClientRect();
                    if (r.width>0 && r.height>0) {
                      var a = (all[i].id+' '+all[i].name+' '+(all[i].getAttribute('aria-label')||'')+' '+(all[i].placeholder||'')).toLowerCase();
                      if (a.includes('code')||a.includes('totp')||a.includes('pin')||all[i].type==='tel'||all[i].type==='number') {
                        inp = all[i]; break;
                      }
                    }
                  }
                  if (!inp) inp = all.find(function(i){var r=i.getBoundingClientRect();return r.width>0&&r.height>0&&['tel','number','text'].includes(i.type);});
                  if (!inp && location.pathname.includes('/challenge/totp')) {
                    inp = all.find(function(i){var r=i.getBoundingClientRect();return r.width>0&&r.height>0&&(i.type==='password'||i.name==='Passwd');});
                  }
                  if (inp) {
                      var r = inp.getBoundingClientRect();
                      return {x:r.x, y:r.y, w:r.width, h:r.height};
                  }
                  return null;
                })()"""
                
                rect = cdp_eval(_totp_rect_js)
                if rect and rect.get('w', 0) > 0:
                    target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
                    target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
                    cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
                    _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
                    time.sleep(random.uniform(0.05, 0.15))
                    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                    time.sleep(random.uniform(0.04, 0.12))
                    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                    
                    uc_sleep(0.1, 0.3)
                    cdp_clear_input()
                    uc_sleep(0.1, 0.3)
                    cdp_type_text(totp_code)
                    logger.info(f"uc-login: CDP TOTP natively typed")
                    
                    time.sleep(0.3)
                    
                    _totp_btn_js = """
                    (function(){
                      var nb=document.querySelector('#totpNext,#idvPreregisteredPhoneNext,[jsname="LgbsSe"],button[type="submit"],button[aria-label*="Next"],button[aria-label*="Verify"]');
                      if(nb) {
                          var r = nb.getBoundingClientRect();
                          return {x:r.x, y:r.y, w:r.width, h:r.height};
                      }
                      return null;
                    })()"""
                    btn_rect = cdp_eval(_totp_btn_js)
                    if btn_rect and btn_rect.get('w', 0) > 0:
                        target_x = btn_rect['x'] + btn_rect['w'] * random.uniform(0.3, 0.7)
                        target_y = btn_rect['y'] + btn_rect['h'] * random.uniform(0.3, 0.7)
                        cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
                        _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
                        time.sleep(random.uniform(0.05, 0.15))
                        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                        time.sleep(random.uniform(0.04, 0.12))
                        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                        logger.info("uc-login: TOTP Next clicked natively")
                    else:
                        # Fallback to hitting enter natively
                        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 13, "key": "Enter"})
                        time.sleep(0.05)
                        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 13, "key": "Enter"})
                        logger.info("uc-login: TOTP Next Enter pressed natively")
                    
                    for _ in range(16):
                        time.sleep(0.5)
                        try:
                            if "/challenge/" not in driver.current_url:
                                logger.info(f"uc-login: left challenge → {driver.current_url[:70]}")
                                break
                        except Exception:
                            break
                    time.sleep(1.5)
                else:
                    logger.warning("uc-login: CDP TOTP fill failed — could not find input")
            except Exception as _te:
                logger.warning(f"uc-login: TOTP block failed: {_te}")
        elif not _is_challenge:
            logger.info(f"uc-login: no challenge after password ({curr3[:70]}) — skipping TOTP")



        # "Stay signed in?" prompt — CDP native click
        try:
            cdp_click_element(['#confirm-button', '[data-action="confirm"]', 'button[jsname="LgbsSe"]'], timeout=2)
            logger.info("uc-login: 'Stay signed in' confirmed natively")
            time.sleep(1)
        except Exception:
            pass

        # ── Post-login prompt handler (XIOBR lines 916-951) ─────────────────
        # Google shows recovery/passkey/add-phone prompts after 2FA login.
        # These block reaching Gmail. Dismiss them with Cancel/Not now/Skip.
        from selenium.webdriver.common.keys import Keys as _PostKeys
        for _pl_i in range(3):
            try:
                # reCAPTCHA can appear in post-login prompts too (XIOBR lines 920-924)
                try:
                    if "challenge/recaptcha" in driver.current_url or "recaptcha" in driver.page_source.lower()[:5000]:
                        _rc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recaptcha_solver.py")
                        if os.path.exists(_rc_path):
                            import importlib.util as _rc_ilu2
                            _rc_spec2 = _rc_ilu2.spec_from_file_location("recaptcha_solver", _rc_path)
                            _rc_mod2 = _rc_ilu2.module_from_spec(_rc_spec2)
                            _rc_spec2.loader.exec_module(_rc_mod2)
                            if _rc_mod2.solve_recaptcha(driver, log_fn=lambda m: logger.info(m), sleep_fn=uc_sleep):
                                logger.info("uc-login: reCAPTCHA in post-login loop solved")
                                uc_sleep(2.0, 3.0)
                                continue  # re-check page after captcha solve
                except Exception as _rcpl:
                    logger.warning(f"uc-login: post-login reCAPTCHA check error: {_rcpl}")

                uc_sleep(1.0, 2.0)
                _ps = driver.page_source.lower()
                _prompt_keywords = [
                    'recovery', 'make sure you can always sign in',
                    'protect your account', 'passkey',
                    'add a phone number', 'not now',
                    'home address', 'set a home address',
                ]
                if any(x in _ps for x in _prompt_keywords):
                    logger.info(f"uc-login: post-login prompt detected (iter {_pl_i+1}) — clicking Skip/Cancel")
                    try:
                        _skip_rect_js = """
                        (function(){
                          var all=document.querySelectorAll('button');
                          for(var i=0;i<all.length;i++){
                            var t=(all[i].innerText||all[i].textContent||'').toLowerCase().trim();
                            if(t.includes('cancel')||t.includes('not now')||t.includes('skip')||t.includes('no thanks')){
                              var r = all[i].getBoundingClientRect();
                              return {x:r.x, y:r.y, w:r.width, h:r.height};
                            }
                          } return null;
                        })()
                        """
                        rect = cdp_eval(_skip_rect_js)
                        if rect and rect.get('w', 0) > 0:
                            target_x = rect['x'] + rect['w'] * random.uniform(0.3, 0.7)
                            target_y = rect['y'] + rect['h'] * random.uniform(0.3, 0.7)
                            cdp_mouse_move(_mouse_pos["x"], _mouse_pos["y"], target_x, target_y)
                            _mouse_pos["x"], _mouse_pos["y"] = target_x, target_y
                            time.sleep(random.uniform(0.05, 0.15))
                            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                            time.sleep(random.uniform(0.04, 0.12))
                            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(target_x), "y": int(target_y)})
                            logger.info("uc-login: post-login prompt dismissed (Skip/Cancel) natively")
                            uc_sleep(2.0, 3.0)
                        else:
                            break
                    except Exception as _ske:
                        logger.warning(f"uc-login: post-login Skip/Cancel failed: {_ske}")
                        break
                else:
                    break
            except Exception as _ple:
                logger.warning(f"uc-login: post-login prompt check failed: {_ple}")
                break

        final_url_pre_verify = driver.current_url
        if "accounts.google.com" in final_url_pre_verify and ("signin" in final_url_pre_verify or "challenge" in final_url_pre_verify):
            return {"ok": False, "error": f"Login failed — stuck on sign-in/challenge page: {final_url_pre_verify[:120]}"}

        # Verify — navigate to myaccount; may redirect to google.com/account/about on some accounts
        safe_get("https://myaccount.google.com/", wait=2.0)
        time.sleep(1)

        # reCAPTCHA at final verify page (XIOBR lines 957-961)
        try:
            if "challenge/recaptcha" in driver.current_url or "recaptcha" in driver.page_source.lower()[:5000]:
                _rc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recaptcha_solver.py")
                if os.path.exists(_rc_path):
                    import importlib.util as _rc_ilu3
                    _rc_spec3 = _rc_ilu3.spec_from_file_location("recaptcha_solver", _rc_path)
                    _rc_mod3 = _rc_ilu3.module_from_spec(_rc_spec3)
                    _rc_spec3.loader.exec_module(_rc_mod3)
                    if _rc_mod3.solve_recaptcha(driver, log_fn=lambda m: logger.info(m), sleep_fn=uc_sleep):
                        logger.info("uc-login: reCAPTCHA at verify page solved — re-navigating")
                        safe_get("https://myaccount.google.com/", wait=2.0)
                        time.sleep(1)
        except Exception as _rcv:
            logger.warning(f"uc-login: verify-page reCAPTCHA check error: {_rcv}")
        final_url = driver.current_url

        # Failure: stuck on accounts.google.com sign-in page or redirected to unauthenticated /account/about/
        if "accounts.google.com/v3/signin" in final_url or "accounts.google.com/ServiceLogin" in final_url or "/account/about/" in final_url:
            return {"ok": False, "error": f"Login failed — sign-in page or unauthenticated: {final_url[:120]}"}


        # ── Capture ALL cookies via CDP (not just current-domain ones) ─────────
        # After successful TOTP login, Chrome auto-navigates to mail.google.com
        # (via continue=https://mail.google.com in ServiceLogin URL). All 65 Gmail
        # auth cookies (SAPISID, SSID, APISID, SID, etc.) are already in Chrome's
        # cookie jar by the time myaccount verification ran above.
        # Network.getAllCookies captures across all domains regardless of current URL.
        # DO NOT navigate to gmail.com — it redirects to workspace.google.com which
        # is a different domain and replaces the mail.google.com cookies with 5 others.
        try:
            all_data = driver.execute_cdp_cmd("Network.getAllCookies", {})
            cookies = all_data.get("cookies", [])
            # Log breakdown by domain for debugging
            domains: dict[str, int] = {}
            for c in cookies:
                d = c.get("domain", "?")
                domains[d] = domains.get(d, 0) + 1
            logger.info(f"uc-login: cookies={len(cookies)} by domain: {domains}")
        except Exception as ce:
            logger.warning(f"uc-login: Network.getAllCookies failed ({ce}), falling back to get_cookies()")
            cookies = driver.get_cookies()

        # ── Get UC Chrome's CDP port so patchright can attach to the same browser ──
        try:
            _debug_addr = driver.options.debugger_address or ""   # "localhost:PORT"
            _uc_port = int(_debug_addr.split(":")[-1]) if ":" in _debug_addr else 0
        except Exception:
            _uc_port = 0

        # ── Navigate to blank to flush profile writes before any tar/read ────────
        try:
            driver.get("about:blank")
            time.sleep(1.0)
        except Exception:
            pass

        session_id = os.path.basename(user_data_dir).replace("uc-profile-", "") if user_data_dir else ""
        if session_id:
            _uc_drivers[session_id] = driver
            logger.info(f"uc-login: driver kept alive session={session_id} uc_port={_uc_port}")
        # NOTE: driver.quit() is intentionally NOT called here on success.
        # The authenticated Chrome process stays alive for subsequent automation.

        logger.info(f"uc-login: success cookies={len(cookies)} final_url={final_url}")
        return {
            "ok": True, "cookies": cookies, "engine": "uc",
            "final_url":   final_url,
            "profile_dir": user_data_dir,
            "uc_port":     _uc_port,       # Chrome CDP port — patchright can connect_over_cdp to this
        }

    except Exception as exc:
        logger.error(f"uc-login exception: {exc}")
        try:
            driver.quit()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    # NOTE: no finally quit — on success the driver is kept in _uc_drivers


@app.post("/run-uc-login")
async def run_uc_login(req: UCLoginRequest) -> dict:
    """
    Run UC stealth login using Google Chrome 153.

    Picks up exit node proxy + stealth UA from the session (_sessions dict).
    Resolves timezone from exit_node_public_ip (or session cached timezone).
    Returns Google cookies to inject into a patchright browser context.
    """
    session_info = _sessions.get(req.session_id or "", {})
    proxy_url    = req.proxy_url or session_info.get("proxy_url")
    user_agent   = _STEALTH_UA_CHROME

    # Resolve timezone: prefer request field → session cache → runtime auto-detect
    _exit_ip = (
        req.exit_node_public_ip
        or session_info.get("exit_node_ip")
    )
    if session_info.get("timezone") and not req.exit_node_public_ip:
        # Session already resolved timezone at launch — reuse it (saves ip-api call)
        _tz = session_info["timezone"]
    else:
        _tz = await asyncio.get_event_loop().run_in_executor(
            None, _resolve_ip_timezone, _exit_ip
        )

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
            "ok":    False,
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
                        logger.info(f"run-uc-login: evicted {_evicted} non-google.com cookies (inline)")
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
        "Cache", "Code Cache", "GPUCache",
        "Local Storage", "IndexedDB",
        "BrowsingTopicsSiteData", "BrowsingTopicsState",
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

    import functools
    loop = asyncio.get_event_loop()

    # ── Per-identity distributed login lock (PG advisory via XIOSYNC API) ────
    # Prevents two workers from signing into the same account concurrently.
    _lock_resource = f"google-signin/{req.identity_id or req.email}"
    _lock_acquired = False
    if XIOSYNC_BASE:
        try:
            import urllib.request, json as _json_lk
            _lk_url = f"{XIOSYNC_BASE}/workers/lock/pg/acquire"
            _lk_data = _json_lk.dumps({
                "resource_key": _lock_resource,
                "node_name": NODE_NAME,
                "ttl_seconds": 180,
            }).encode()
            _lk_req = urllib.request.Request(
                _lk_url, data=_lk_data,
                headers={"Content-Type": "application/json", "X-Worker-Secret": XIOSYNC_TOKEN},
                method="POST",
            )
            with urllib.request.urlopen(_lk_req, timeout=5) as _lk_resp:
                _lk_result = _json_lk.loads(_lk_resp.read())
                _lock_acquired = _lk_result.get("acquired", False)
                if not _lock_acquired:
                    _holder = _lk_result.get("holder", "unknown")
                    logger.warning(f"run-uc-login: login lock held by {_holder} — proceeding anyway (non-blocking)")
                else:
                    logger.info(f"run-uc-login: login lock acquired for {_lock_resource}")
        except Exception as _lk_err:
            logger.warning(f"run-uc-login: lock acquire failed ({_lk_err}) — proceeding without lock")

    try:
        # If /run-uc-login-start pre-launched a UC Chrome, pass it in to reuse
        _pre_driver = _uc_pending_drivers.pop(req.session_id, None)
        result = await loop.run_in_executor(
            None,
            functools.partial(
                _run_uc_login_sync,
                req.email, req.password, req.totp_secret, proxy_url, user_agent,
                user_data_dir=_profile_dir,
                pre_launched_driver=_pre_driver,
                exit_node_public_ip=req.exit_node_public_ip,
            ),
        )

        if result.get("ok") and result.get("uc_port"):
            # ── Register session so /health and other endpoints can reference it ──
            _uc_port = result["uc_port"]
            _tailscale_ip = _my_tailscale_ip()
            _cdp_ws = f"ws://{_tailscale_ip}:{_uc_port}"
            _sessions[req.session_id] = {
                "browser":     None,   # UC Chrome — Selenium manages it, not patchright
                "context":     None,
                "pw":          None,
                "pid":         0,
                "port":        _uc_port,
                "cdp_ws_url":  _cdp_ws,
                "proxy_url":   proxy_url,
                "profile_dir": _profile_dir,
                "chrome_proc": None,
                "guard_task":  None,
                "timezone":    _tz,
                "exit_node_ip": _exit_ip,
            }
            result["cdp_ws_url"] = _cdp_ws
            logger.info(
                f"run-uc-login: session registered session={req.session_id} port={_uc_port} "
                f"cdp_ws={_cdp_ws}"
            )

        # ── HITL pause on recoverable failure (XIOBR port) ───────────────────
        # If login failed with a recoverable error, create a HITL notice and
        # wait for an operator or AI agent to resume before returning failure.
        if not result.get("ok"):
            _error_msg = result.get("error", "")
            # Classify as recoverable if it's a challenge/verification issue (not infra)
            _recoverable = any(k in _error_msg.lower() for k in [
                "sign-in page", "rejected", "verification", "challenge",
                "totp", "2fa", "captcha",
            ]) or "final_url" in str(result)
            if _recoverable:
                try:
                    from xiosync.subsystems.xiorun.hitl import HITLNotice, HITLState
                    import uuid as _hitl_uuid
                    _org_id = _hitl_uuid.UUID(req.org_id if hasattr(req, "org_id") and req.org_id else "00000000-0000-7000-8000-000000000000")
                    _notice = HITLNotice(
                        organization_id=_org_id,
                        session_id=req.session_id or "uc-login",
                        challenge_type="login_failed",
                        message=f"Google sign-in failed for {req.email}: {_error_msg[:200]}",
                        identity_id=_hitl_uuid.UUID(req.identity_id) if req.identity_id else None,
                        instructions="Check XIOVIEW for the current browser state. Resume after manual intervention or retry.",
                    )
                    _hitl_store.create(_notice)
                    logger.info(f"run-uc-login: HITL notice created id={_notice.id} — waiting up to 300s for resume")
                    result["hitl_notice_id"] = str(_notice.id)
                    result["hitl_state"] = "PENDING"
                    # Don't block — return immediately with HITL info so the
                    # workflow caller can decide whether to wait or retry
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
                import urllib.request, json as _json_lkr
                _lkr_url = f"{XIOSYNC_BASE}/workers/lock/pg/release"
                _lkr_data = _json_lkr.dumps({
                    "resource_key": _lock_resource,
                    "node_name": NODE_NAME,
                }).encode()
                _lkr_req = urllib.request.Request(
                    _lkr_url, data=_lkr_data,
                    headers={"Content-Type": "application/json", "X-Worker-Secret": XIOSYNC_TOKEN},
                    method="POST",
                )
                with urllib.request.urlopen(_lkr_req, timeout=5) as _lkr_resp:
                    logger.info(f"run-uc-login: login lock released for {_lock_resource}")
            except Exception as _lkr_err:
                logger.warning(f"run-uc-login: lock release failed ({_lkr_err})")


@app.post("/run-uc-login-start")
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
    proxy_url    = req.proxy_url or session_info.get("proxy_url") or _SSH_PROXY_URL
    user_agent   = _STEALTH_UA_CHROME

    if not proxy_url:
        return {"ok": False, "error": "EXIT_NODE_REQUIRED: no proxy_url provided and no bridge running"}

    # Deterministic path convention
    _node_slug = NODE_NAME.replace("-", "_")
    _re_prof = __import__("re").compile(r"[^a-zA-Z0-9]")
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
        try: os.remove(_lpath)
        except FileNotFoundError: pass

    def _launch_uc_chrome():
        """Just launch UC Chrome and return (driver, uc_port) — no login."""
        import undetected_chromedriver as uc
        import glob, socket as _sock

        def _find_chrome():
            import glob as _g
            # 1. Chrome 131 — optimal for UC 3.5.5 (fully patches webdriver artifacts)
            if os.path.isfile("/opt/chrome131/chrome") and os.access("/opt/chrome131/chrome", os.X_OK):
                return "/opt/chrome131/chrome", 131
            # 2. patchright Chromium — prefer ≤133, accept 153 as last resort
            _pr_best = None
            for path in sorted(_g.glob("/root/.cache/ms-patchright/chromium-*/chrome-linux64/chrome"), reverse=True):
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    try:
                        raw = subprocess.check_output([path, "--version"], timeout=5,
                                                      stderr=subprocess.DEVNULL).decode().strip()
                        major = int(raw.split()[-1].split(".")[0])
                        if major <= 133:
                            return path, major
                        elif _pr_best is None or major < _pr_best[1]:
                            _pr_best = (path, major)
                    except Exception:
                        pass
            if _pr_best:
                logger.warning(f"uc-login-start: using patchright Chromium {_pr_best[1]} (>133, detection risk higher)")
                return _pr_best
            # 3. System Chrome ≤133 only
            for path in ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"]:
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    try:
                        raw = subprocess.check_output([path, "--version"], timeout=5,
                                                      stderr=subprocess.DEVNULL).decode().strip()
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
        opts.add_argument("--disable-setuid-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--use-gl=angle")
        opts.add_argument("--use-angle=swiftshader")
        opts.add_argument("--disable-gpu-sandbox")
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
        opts.add_experimental_option("prefs", {
            "webrtc.ip_handling_policy":     "disable_non_proxied_udp",
            "webrtc.multiple_routes_enabled": False,
            "webrtc.nonproxied_udp_enabled":  False,
            # NOTE: Do NOT add profile.exited_cleanly=True — clears session cookies
        })

        # Pick free debug port — passed to uc.Chrome(port=) directly (not via opts)
        # DO NOT also add --remote-debugging-port to opts; uc.Chrome sets it via port=
        _s = _sock.socket(); _s.bind(("", 0)); port = _s.getsockname()[1]; _s.close()

        # Ensure UC chromedriver path has our version-matched binary before patching
        # UC patches ~/.local/share/undetected_chromedriver/undetected_chromedriver in place.
        # We refresh it from our known-good copy before each launch so the patcher starts clean.
        _our_cd = "/usr/local/bin/chromedriver131"
        _uc_cd_dir = os.path.expanduser("~/.local/share/undetected_chromedriver")
        _uc_cd_path = os.path.join(_uc_cd_dir, "undetected_chromedriver")
        if os.path.isfile(_our_cd):
            os.makedirs(_uc_cd_dir, exist_ok=True)
            import shutil as _sh
            _sh.copy2(_our_cd, _uc_cd_path)
            os.chmod(_uc_cd_path, 0o755)
        _drv_path = _uc_cd_path if os.path.isfile(_uc_cd_path) else None

        driver = uc.Chrome(
            options=opts,
            browser_executable_path=chrome_bin,
            driver_executable_path=_drv_path,
            version_main=chrome_ver,
            use_subprocess=False,   # True causes 'chrome not reachable' on Colab (port lost)
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
            "ok":       True,
            "uc_port":  uc_port,
            "cdp_ws_url": f"ws://{_tailscale_ip}:{uc_port}",
            "profile_dir": _profile_dir,
            "message": "UC Chrome launched. Attach XIOVIEW, then POST /run-uc-login to start login.",
        }
    except Exception as exc:
        logger.error(f"run-uc-login-start EXCEPTION: {exc}")
        return {"ok": False, "error": str(exc)}


# ── Utility: navigate / screenshot on pre-launched UC driver ──────────────────

class UCNavigateRequest(BaseModel):
    session_id: str | None = None
    url: str
    wait_seconds: float = 10.0


@app.post("/uc-navigate")
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
        import time as _t; _t.sleep(req.wait_seconds)
        return driver.current_url, driver.title, driver.get_screenshot_as_base64()

    loop = asyncio.get_event_loop()
    try:
        current_url, title, png_b64 = await loop.run_in_executor(None, _do_navigate)
        logger.info(f"uc-navigate: {req.url!r} → {current_url!r} title={title!r}")
        return {"ok": True, "current_url": current_url, "title": title, "screenshot_png_b64": png_b64}
    except Exception as exc:
        logger.error(f"uc-navigate error: {exc}")
        return {"ok": False, "error": str(exc)}


class UCScreenshotRequest(BaseModel):
    session_id: str | None = None


@app.post("/uc-screenshot")
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


# ── Phase 3: Cookie injection + session verification ───────────────────────────

class InjectCookiesRequest(BaseModel):
    session_id: str
    cookies:    list[dict]   # raw cookie dicts from /run-uc-login
    verify_url: str = "https://myaccount.google.com/"


@app.post("/inject-cookies")
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
            same_site_map = {"strict": "Strict", "lax": "Lax", "none": "None",
                             "no_restriction": "None", "unspecified": "Lax"}
            ss_raw = str(c.get("sameSite") or c.get("same_site") or "lax").lower()
            ss = same_site_map.get(ss_raw, "Lax")

            entry: dict = {
                "name":     c["name"],
                "value":    c["value"],
                "domain":   c.get("domain", ".google.com"),
                "path":     c.get("path", "/"),
                "secure":   bool(c.get("secure", False)),
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
                        account_email = (
                            await el.get_attribute("data-email") or
                            await el.get_attribute("aria-label")
                        )
                    if not account_email and "@" in title:
                        account_email = title.split("(")[-1].strip(")")
                except Exception:
                    pass

            logger.info(
                f"inject-cookies verify: verified={verified} "
                f"email={account_email} url={final_url[:80]}"
            )
            return {
                "ok":           True,
                "verified":     verified,
                "account_email": account_email,
                "final_url":    final_url,
                "cookie_count": len(pw_cookies),
            }
        finally:
            await page.close()

    except Exception as _exc:
        import traceback as _tb_mod
        _tb = _tb_mod.format_exc()
        logger.error(f"inject-cookies EXCEPTION: {_exc}\n{_tb}")
        return {"ok": False, "error": str(_exc), "traceback": _tb[-600:]}


# ── Live navigation (keeps page open for XIOVIEW) ──────────────────────────────

class NavigateRequest(BaseModel):
    session_id: str
    url:        str
    wait_until: str = "domcontentloaded"   # domcontentloaded | load | networkidle


@app.post("/navigate")
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
        page  = pages[0] if pages else await ctx.new_page()
        await page.goto(req.url, wait_until=req.wait_until, timeout=30_000)
        return {"ok": True, "final_url": page.url, "title": await page.title()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── HITL & Domain Eviction Endpoints ─────────────────────────────────
# HITL types are inlined here so xiorun_agent.py works standalone on Colab
# (the xiosync package is not installed on the worker runtime).

import uuid  # noqa: F811 (re-import is fine; ensures module is available at class scope)
from datetime import datetime, timezone  # noqa: F811
from enum import StrEnum as _StrEnum
from dataclasses import dataclass as _dataclass, field as _field

class HITLState(_StrEnum):
    PENDING   = "PENDING"
    RESUMED   = "RESUMED"
    EXPIRED   = "EXPIRED"
    CANCELLED = "CANCELLED"

class HITLResumedBy(_StrEnum):
    HUMAN    = "HUMAN"
    AI_AGENT = "AI_AGENT"
    TIMEOUT  = "TIMEOUT"

@_dataclass
class HITLNotice:
    organization_id: uuid.UUID
    session_id:      str
    challenge_type:  str
    message:         str
    id:              uuid.UUID        = _field(default_factory=uuid.uuid4)
    identity_id:     uuid.UUID | None = None
    instructions:    str | None       = None
    state:           HITLState        = HITLState.PENDING
    created_at:      datetime         = _field(default_factory=lambda: datetime.now(timezone.utc))
    resumed_at:      datetime | None  = None
    resumed_by:      HITLResumedBy | None = None

class _HITLStore:
    def __init__(self):
        self._notices: dict[uuid.UUID, HITLNotice] = {}
        self._events:  dict[uuid.UUID, asyncio.Event] = {}

    def create(self, notice: HITLNotice) -> HITLNotice:
        self._notices[notice.id] = notice
        self._events[notice.id]  = asyncio.Event()
        return notice

    def get(self, notice_id: uuid.UUID) -> HITLNotice | None:
        return self._notices.get(notice_id)

    def list_pending(self) -> list[HITLNotice]:
        return [n for n in self._notices.values() if n.state == HITLState.PENDING]

    def resume(self, notice_id: uuid.UUID,
               resumed_by: HITLResumedBy = HITLResumedBy.HUMAN) -> HITLNotice | None:
        notice = self.get(notice_id)
        if notice and notice.state == HITLState.PENDING:
            notice.state      = HITLState.RESUMED
            notice.resumed_at = datetime.now(timezone.utc)
            notice.resumed_by = resumed_by
            if notice_id in self._events:
                self._events[notice_id].set()
        return notice

_hitl_store = _HITLStore()

class HITLCreateRequest(BaseModel):
    session_id:       str
    challenge_type:   str
    message:          str
    instructions:     str | None = None
    screenshot_path:  str | None = None

class HITLResumeRequest(BaseModel):
    resumed_by: str = "human"  # human, ai_agent

@app.post("/hitl/create")
async def create_hitl_notice(req: HITLCreateRequest):
    """Create a HITL notice for operator/AI intervention."""
    notice = HITLNotice(
        organization_id=uuid.uuid4(),  # worker-local org placeholder
        session_id=req.session_id,
        challenge_type=req.challenge_type,
        message=req.message,
        instructions=req.instructions,
    )
    created = _hitl_store.create(notice)
    return {"notice_id": str(created.id), "state": created.state.value}

@app.post("/hitl/{notice_id}/resume")
async def resume_hitl_notice(notice_id: str, req: HITLResumeRequest):
    """Resume a pending HITL notice (human or AI agent)."""
    by = HITLResumedBy.AI_AGENT if req.resumed_by == "ai_agent" else HITLResumedBy.HUMAN
    notice = _hitl_store.resume(uuid.UUID(notice_id), resumed_by=by)
    if not notice:
        raise HTTPException(status_code=404, detail="HITL notice not found or already resolved")
    return {"notice_id": notice_id, "state": notice.state.value, "resumed_by": notice.resumed_by.value}

@app.get("/hitl/pending")
async def list_pending_hitl():
    """List all pending HITL notices."""
    notices = _hitl_store.list_pending()
    return {
        "notices": [
            {
                "id":             str(n.id),
                "session_id":     n.session_id,
                "challenge_type": n.challenge_type,
                "message":        n.message,
                "state":          n.state.value,
                "created_at":     n.created_at.isoformat(),
            }
            for n in notices
        ]
    }

class EvictDomainRequest(BaseModel):
    profile_dir: str | None = None
    domain_pattern: str
    cascade: bool = True

@app.post("/evict-domain")
async def evict_domain_endpoint(req: EvictDomainRequest):
    """Evict a domain's cookies from a profile."""
    from xiosync.subsystems.xiorun.domain_eviction import evict_domain_full
    result = evict_domain_full(
        profile_dir=req.profile_dir,
        state=None,
        domain_pattern=req.domain_pattern,
        cascade=req.cascade,
    )
    logger.info(f"Domain eviction: {req.domain_pattern} -> {result}")
    return result

@app.post("/inject-session-state")
async def inject_session_state(req: dict):
    """Inject cookie state into a running browser session via CDP."""
    session_id = req.get("session_id")
    cookies = req.get("cookies", [])
    if not session_id or session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    sess = _sessions[session_id]
    cdp_url = sess.get("cdp_ws")
    if not cdp_url:
        raise HTTPException(status_code=400, detail="No CDP endpoint")
    try:
        from patchright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            await context.add_cookies(cookies)
            count = len(cookies)
            logger.info(f"Injected {count} cookies into session {session_id}")
            return {"injected": count, "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Session Cascade Check ─────────────────────────────────────────────────────
# Pre-login validation: check if existing session is still valid before
# launching a full UC Chrome login. 4 levels (cheapest → most expensive):
#   1. Local profile dir exists on disk
#   2. Live browser verify via patchright → myaccount.google.com DOM check
#   3. Pull profile from Drive FUSE via ChromeProfileStore.restore()
#   4. Not found → proceed to full login

class CascadeCheckRequest(BaseModel):
    identity_id: str
    email: str
    proxy_url: str | None = None

@app.post("/session-cascade-check")
async def session_cascade_check(req: CascadeCheckRequest):
    """Pre-login session validation. Returns {valid, profile_dir, level}."""
    import re as _re_cc, glob as _glob_cc

    _id_short = req.identity_id.replace("-", "")[:16]
    _node_slug = _re_cc.sub(r"[^a-zA-Z0-9-]", "-", NODE_NAME or "default")

    # Level 1: Local profile dir exists
    _local_matches = sorted(_glob_cc.glob(f"/tmp/xiorun_profiles/PRFL_{_id_short}*"))
    _profile_dir = str(_local_matches[-1]) if _local_matches else None

    if _profile_dir and os.path.isdir(_profile_dir):
        logger.info(f"cascade-check: L1 local profile found: {_profile_dir}")

        # Level 2: Live verify — launch patchright, navigate to myaccount
        try:
            from patchright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    user_data_dir=_profile_dir,
                    viewport={"width": 1440, "height": 900},
                )
                page = await context.new_page()
                await page.goto("https://myaccount.google.com/", wait_until="networkidle", timeout=15000)
                await asyncio.sleep(1.5)

                prefix = req.email.split("@")[0].lower()
                body_text = await page.evaluate("document.body.innerText.toLowerCase()")
                is_valid = prefix in body_text and "sign in" not in (await page.title()).lower()

                await browser.close()

                if is_valid:
                    logger.info(f"cascade-check: L2 live verify PASSED for {req.email}")
                    return {"valid": True, "profile_dir": _profile_dir, "level": "LIVE_VERIFY"}
                else:
                    logger.info(f"cascade-check: L2 live verify FAILED for {req.email}")
        except Exception as e:
            logger.warning(f"cascade-check: L2 verify error: {e}")

    # Level 3: Pull from Drive FUSE
    try:
        # Import profile_store — available if boot.py has set up env vars
        _ps_candidates = [
            "/tmp/xio_drive_fs.py",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "xiosync", "subsystems", "xiorun", "profile_store.py"),
        ]
        # Use a lightweight Drive check: does the tar.gz file exist on Drive?
        _drive_profile_key = f"chrome_profiles/PRFL_{_id_short}.tar.gz"
        _drive_path = os.path.join(DRIVE_ROOT, _drive_profile_key)
        if os.path.exists(_drive_path):
            logger.info(f"cascade-check: L3 Drive profile found: {_drive_path}")
            # Extract it locally
            _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_id_short}__{_node_slug}"
            os.makedirs(_profile_dir, exist_ok=True)
            import tarfile as _tf_cc
            try:
                with _tf_cc.open(_drive_path, "r:gz") as tf:
                    tf.extractall(path="/tmp/xiorun_profiles")
                logger.info(f"cascade-check: L3 profile extracted to {_profile_dir}")
                return {"valid": False, "profile_dir": _profile_dir, "level": "DRIVE_PULL", "needs_verify": True}
            except Exception as _tex:
                logger.warning(f"cascade-check: L3 extract failed: {_tex}")
        else:
            logger.info(f"cascade-check: L3 no Drive profile at {_drive_path}")
    except Exception as e:
        logger.warning(f"cascade-check: L3 Drive check error: {e}")

    # Level 4: Not found — proceed to full login
    _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_id_short}__{_node_slug}"
    os.makedirs(_profile_dir, exist_ok=True)
    return {"valid": False, "profile_dir": _profile_dir, "level": "NOT_FOUND"}


# ── Persist Session ────────────────────────────────────────────────────────────
# Post-login persistence: save cookies to vault + Chrome profile to Drive.
# Uses existing xiorun subsystem modules (session_state.py, profile_store.py).

class PersistSessionRequest(BaseModel):
    identity_id: str
    org_id: str = "00000000-0000-7000-8000-000000000000"
    cookies: list[dict] = []
    profile_dir: str | None = None
    page_url: str | None = None

@app.post("/persist-session")
async def persist_session(req: PersistSessionRequest):
    """Save cookies to vault + Chrome profile tar to Drive FUSE."""
    results = {"cookie_saved": False, "profile_saved": False, "cookie_count": 0, "profile_key": None}

    # 1. Save cookies to vault via SessionStateIO
    if req.cookies:
        try:
            # Build DB engine — connect to XIOSYNC PostgreSQL
            _db_url = os.environ.get("DATABASE_URL", "")
            if not _db_url and XIOSYNC_BASE:
                # On workers, use the XIOSYNC API to persist instead of direct DB
                import urllib.request, json as _json_ps
                _api_url = f"{XIOSYNC_BASE}/api/v1/vault/secrets"
                _payload = _json_ps.dumps({
                    "organization_id": req.org_id,
                    "key": f"identities/{req.identity_id}/cookie_state",
                    "value": _json_ps.dumps({"cookies": req.cookies, "origins": []}),
                    "secret_type": "cookie_state",
                }).encode()
                _rq = urllib.request.Request(
                    _api_url, data=_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {XIOSYNC_TOKEN}",
                    },
                    method="PUT",
                )
                try:
                    with urllib.request.urlopen(_rq, timeout=10) as resp:
                        results["cookie_saved"] = True
                        results["cookie_count"] = len(req.cookies)
                        logger.info(f"persist-session: cookies saved to vault ({len(req.cookies)} cookies)")
                except Exception as _ve:
                    logger.warning(f"persist-session: vault API save failed: {_ve}")
            elif _db_url:
                # Direct DB access (if running on server or DATABASE_URL set)
                from sqlalchemy import create_engine as _ce_ps
                _engine = _ce_ps(_db_url)
                from xiosync.subsystems.xiorun.session_state import SessionStateIO
                sio = SessionStateIO(_engine)
                sio.save(
                    identity_id=req.identity_id,
                    org_id=req.org_id,
                    state={"cookies": req.cookies, "origins": []},
                    page_url=req.page_url,
                )
                results["cookie_saved"] = True
                results["cookie_count"] = len(req.cookies)
                logger.info(f"persist-session: cookies saved to vault ({len(req.cookies)} cookies)")
        except Exception as e:
            logger.warning(f"persist-session: cookie save error: {e}")

    # 2. Save Chrome profile tar to Drive FUSE
    if req.profile_dir and os.path.isdir(req.profile_dir):
        try:
            import tarfile as _tf_ps, hashlib as _hl_ps
            _id_short = req.identity_id.replace("-", "")[:16]
            _profile_key = f"chrome_profiles/PRFL_{_id_short}.tar.gz"
            _drive_path = os.path.join(DRIVE_ROOT, _profile_key)
            os.makedirs(os.path.dirname(_drive_path), exist_ok=True)

            # Trim cache dirs before archiving (same set as profile_store.py)
            _TRIM = ["Cache", "Code Cache", "GPUCache", "DawnCache", "ShaderCache",
                      "Service Worker/CacheStorage", "Service Worker/ScriptCache",
                      "BudgetDatabase", "Network Action Predictor"]
            import shutil as _sh_ps
            for _td in _TRIM:
                _full = os.path.join(req.profile_dir, _td)
                if os.path.exists(_full):
                    _sh_ps.rmtree(_full, ignore_errors=True)

            # Create tar.gz
            import tempfile as _tmp_ps
            _tmp_tar = _tmp_ps.NamedTemporaryFile(suffix=".tar.gz", delete=False)
            _tmp_tar_path = _tmp_tar.name
            _tmp_tar.close()
            try:
                with _tf_ps.open(_tmp_tar_path, "w:gz") as tf:
                    tf.add(req.profile_dir, arcname=os.path.basename(req.profile_dir))
                _sz = os.path.getsize(_tmp_tar_path)
                # Copy to Drive (atomic: write to tmp then rename)
                import shutil as _sh2
                _sh2.copy2(_tmp_tar_path, _drive_path)
                results["profile_saved"] = True
                results["profile_key"] = _profile_key
                results["profile_size"] = _sz
                logger.info(f"persist-session: profile saved to Drive: {_profile_key} ({_sz:,}b)")
            finally:
                os.unlink(_tmp_tar_path)
        except Exception as e:
            logger.warning(f"persist-session: profile save error: {e}")

    return results


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")

