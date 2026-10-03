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
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("xiorun_agent")

from contextlib import asynccontextmanager as _acm

# SSH SOCKS5 tunnel process — set when XIORUN_PROXY_SSH_HOST is configured
_ssh_tunnel_proc: Any = None
_SSH_PROXY_URL: str | None = None   # set at startup: "socks5://127.0.0.1:1056"
_NOVNC_URL:     str | None = None   # set at startup: "http://{tailscale_ip}:6080/vnc.html"

@_acm
async def _lifespan(application):
    """Kill orphan Chrome processes; start SSH SOCKS5 tunnel + noVNC if configured."""
    global _ssh_tunnel_proc, _SSH_PROXY_URL, _NOVNC_URL

    import subprocess as _sp, time as _t

    # ── Kill orphan Chrome/ChromeDriver from prior agent runs ─────────────────
    for _sig in ("-TERM", "-KILL"):
        try:
            _sp.run(["pkill", _sig, "-f", "chrome-linux64/chrome"], capture_output=True)
            _sp.run(["pkill", _sig, "-f", "undetected_chromedriver"],  capture_output=True)
        except Exception:
            pass
    _t.sleep(1)

    # ── noVNC: auto-start x11vnc + websockify for direct HITL control ─────────
    # x11vnc mirrors Xvfb :99 → VNC :5900 → websockify :6080 → noVNC HTML
    # Gives operators a zero-relay direct X11 stream (50ms latency vs 500ms+ CDP)
    try:
        _sp.run(["pkill", "-f", "x11vnc"], capture_output=True)
        _sp.run(["pkill", "-f", "websockify"], capture_output=True)
        _t.sleep(0.5)

        if _sp.run(["which", "x11vnc"], capture_output=True).returncode == 0:
            _sp.Popen(
                ["x11vnc", "-display", ":99", "-nopw", "-listen", "0.0.0.0",
                 "-rfbport", "5900", "-forever", "-shared", "-noxdamage", "-bg", "-q"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
        else:
            # x11vnc not installed — try to install silently
            _sp.run(["apt-get", "install", "-y", "-q", "x11vnc", "novnc"],
                    capture_output=True, timeout=60)
            _sp.Popen(
                ["x11vnc", "-display", ":99", "-nopw", "-listen", "0.0.0.0",
                 "-rfbport", "5900", "-forever", "-shared", "-noxdamage", "-bg", "-q"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )

        _novnc_dir = ""
        for _d in ["/usr/share/novnc", "/opt/novnc", "/usr/local/share/novnc"]:
            if os.path.isfile(f"{_d}/vnc.html") or os.path.isfile(f"{_d}/index.html"):
                _novnc_dir = _d
                break

        _ws_args = ["websockify", "6080", "localhost:5900", "--daemon"]
        if _novnc_dir:
            _ws_args = ["websockify", "--web", _novnc_dir, "6080", "localhost:5900", "--daemon"]
        _sp.Popen(_ws_args, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        _t.sleep(1.5)

        # Derive the Tailscale IP from Tailscale binary (not NODE_NAME which has no IP)
        _ts_ip = ""
        try:
            _ts_out = _sp.run(
                ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
            )
            _ts_ip = _ts_out.stdout.strip().split("\n")[0]
        except Exception:
            # Fallback: check /proc/net/if_inet6 or hostname -I for tailscale100 range
            try:
                _ip_out = _sp.run(["hostname", "-I"], capture_output=True, text=True)
                for _ip in _ip_out.stdout.split():
                    if _ip.startswith("100."):
                        _ts_ip = _ip
                        break
            except Exception:
                pass

        if _ts_ip:
            _NOVNC_URL = f"http://{_ts_ip}:6080/vnc.html"
            logger.info(f"lifespan: noVNC started → {_NOVNC_URL}")
        else:
            logger.warning("lifespan: noVNC started but could not determine Tailscale IP")
    except Exception as _e:
        logger.warning(f"lifespan: noVNC startup failed: {_e}")

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

            # ── Self-restarting supervisor for the SOCKS5 bridge ─────────────
            # If the WS connection drops (XIOSYNC restart, network blip), the
            # bridge's serve_forever() exits silently and port 19055 goes dark.
            # This supervisor loop detects exit and restarts with backoff.
            async def _ws_socks5_supervisor():
                _backoff = 1.0
                while True:
                    try:
                        logger.info(f"ws_bridge/supervisor: starting bridge on :{_WS_SOCKS5_PORT}")
                        await _ws_socks5_bridge()
                        logger.warning("ws_bridge/supervisor: bridge exited — restarting...")
                    except asyncio.CancelledError:
                        logger.info("ws_bridge/supervisor: cancelled, stopping")
                        break
                    except Exception as _sup_err:
                        logger.warning(f"ws_bridge/supervisor: bridge crashed ({_sup_err}) — restarting in {_backoff}s")
                    await asyncio.sleep(_backoff)
                    _backoff = min(_backoff * 2, 30.0)  # exponential backoff, cap 30s

            _ws_bridge_task = asyncio.create_task(_ws_socks5_supervisor())
            _t.sleep(0.5)
            _SSH_PROXY_URL = f"socks5://127.0.0.1:{_WS_SOCKS5_PORT}"
            logger.info(
                f"WS SOCKS5 bridge supervisor ready on :{_WS_SOCKS5_PORT} "
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

    # ── SSH pubkey: save to Drive on every agent startup ─────────────────────
    # boot.py saves the pubkey once at boot, but if Drive FUSE wasn't ready
    # then (or if this is a different account's boot), the pubkey is missing.
    # We re-save here on every agent start to ensure all MESH nodes are covered.
    _node_name_ps = os.environ.get("NODE_NAME", os.environ.get("XIO_NODE_NAME", ""))
    _ssh_id_file  = os.environ.get("XIO_NODE_SSH_ID", f"/root/.ssh/{_node_name_ps or 'xio_node_id'}")
    _pubkey_file  = f"{_ssh_id_file}.pub"
    if _node_name_ps and os.path.isfile(_pubkey_file):
        try:
            _pubkey_content = open(_pubkey_file).read().strip()
            _drive_pubkey_path = os.path.join(
                os.environ.get("XIO_DRIVE_ROOT", "/content/drive/MyDrive/XIOSYNC-Shared"),
                "ssh_pubkeys",
                f"{_node_name_ps}.pub",
            )
            os.makedirs(os.path.dirname(_drive_pubkey_path), exist_ok=True)
            # Write only if different (avoid unnecessary Drive writes)
            _existing = ""
            if os.path.isfile(_drive_pubkey_path):
                try:
                    _existing = open(_drive_pubkey_path).read().strip()
                except Exception:
                    pass
            if _existing != _pubkey_content:
                with open(_drive_pubkey_path, "w") as _pkf:
                    _pkf.write(_pubkey_content + "\n")
                logger.info(f"lifespan: saved pubkey {_node_name_ps}.pub to Drive ssh_pubkeys/")
            else:
                logger.debug(f"lifespan: pubkey {_node_name_ps}.pub unchanged, skipped")
        except Exception as _pke:
            logger.warning(f"lifespan: pubkey save failed (non-fatal): {_pke}")

    # ── Inject stealth JS into existing UC Chrome via CDP (Main World) ────────
    # The UC Chrome on :52611 is launched as a subprocess — patchright route
    # interception doesn't apply to it. Inject via Page.addScriptToEvaluateOnNewDocument
    # so all future page loads in that browser get the WebGL/canvas/UA spoof.
    try:
        import urllib.request as _ur
        import websocket as _ws_cdp  # type: ignore
        _uc_cdp_port = int(os.environ.get("XIORUN_UC_CDP_PORT", "52611"))
        _uc_targets = json.loads(
            _ur.urlopen(f"http://127.0.0.1:{_uc_cdp_port}/json", timeout=3).read()
        )
        _uc_pages = [t for t in _uc_targets if t.get("type") == "page"]
        if _uc_pages:
            # Build a minimal stealth JS from PRFL-002 fingerprint
            _fp_st: dict = {}
            for _pid in ["PRFL-002", "PRFL-001"]:
                try:
                    _fp_st = _load_profile_fingerprint(_pid, DRIVE_ROOT) or {}
                    if _fp_st: break
                except Exception: pass
            _wr = _fp_st.get("webgl_renderer", "ANGLE (Apple, Apple M1, OpenGL 4.1)")
            _wv = "Apple" if "Apple" in _wr else "Google Inc. (NVIDIA)"
            _tz_st = _fp_st.get("timezone", "America/New_York")
            # Compute TZ offset (minutes west of UTC) for the profile timezone
            try:
                import datetime as _dt_mod
                import zoneinfo as _zi
                _tz_obj = _zi.ZoneInfo(_tz_st)
                _tz_offset_min = -int(_dt_mod.datetime.now(_tz_obj).utcoffset().total_seconds() // 60)
            except Exception:
                _tz_offset_min = 300  # default EST
            _sj_uc = (
                _STEALTH_JS
                .replace("{WEBGL_V}", _wv).replace("{WEBGL_R}", _wr)
                .replace("{UA}",          _fp_st.get("ua_template", _STEALTH_UA_CHROME))
                .replace("{PLATFORM}",    _fp_st.get("platform",    "Win32"))
                .replace("{TIMEZONE}",    _tz_st)
                .replace("{TZ_OFFSET}",   str(_tz_offset_min))
                .replace("{CANVAS_SEED}", str(_fp_st.get("canvas_seed", 42)))
                .replace("{AUDIO_SEED}",  str(_fp_st.get("audio_seed",  7)))
                .replace("{SCREEN_W}",    str(_fp_st.get("width",   2560)))
                .replace("{SCREEN_H}",    str(_fp_st.get("height",  1600)))
                .replace("{CORES}",       str(_fp_st.get("cores",   10)))
                .replace("{RAM}",         str(_fp_st.get("ram",     16)))
                .replace("'{LOCALE}'",    "'en-US'")
                .replace("{LOCALE}",      "en-US")
                .replace("{LANG}",        "en-US")
                .replace("{CH_PLATFORM}", _fp_st.get("ch_platform", "macOS"))
                .replace("{CH_ARCH}",     _fp_st.get("ch_arch",     "arm"))
                .replace("{LAT}",         str(_fp_st.get("lat",  40.7128)))
                .replace("{LON}",         str(_fp_st.get("lon", -74.0060)))
                .replace("{IS_MOBILE}",   "false")
                .replace("{SCREEN_AH}",   str(_fp_st.get("height", 1600)))
            )
            for _t in _uc_pages[:3]:  # inject into first 3 page targets
                try:
                    _cdp = _ws_cdp.create_connection(_t["webSocketDebuggerUrl"], timeout=5)

                    # Step 1: Register script for all future navigations
                    _cdp.send(json.dumps({
                        "id": 99,
                        "method": "Page.addScriptToEvaluateOnNewDocument",
                        "params": {"source": _sj_uc},
                    }))
                    _cdp.settimeout(3)
                    try: _cdp.recv()
                    except Exception: pass

                    # Step 2: Run stealth JS immediately on the CURRENT page in-place
                    # This patches the already-loaded page's WebGL/canvas/navigator prototypes
                    _cdp.send(json.dumps({
                        "id": 100,
                        "method": "Runtime.evaluate",
                        "params": {"expression": _sj_uc, "returnByValue": False},
                    }))
                    _cdp.settimeout(4)
                    try:
                        _ev_r = json.loads(_cdp.recv())
                        _ev_err = _ev_r.get("result", {}).get("exceptionDetails")
                        if _ev_err:
                            logger.debug(f"lifespan: stealth evaluate error: {str(_ev_err)[:120]}")
                    except Exception: pass

                    # Step 3: Cold-navigate: about:blank → original URL
                    # addScriptToEvaluateOnNewDocument only fires on FRESH navigations,
                    # not on pages already loaded at registration time. Force one reload.
                    _orig_url = _t.get("url", "")
                    if _orig_url and not _orig_url.startswith("chrome"):
                        _cdp.send(json.dumps({
                            "id": 101,
                            "method": "Page.navigate",
                            "params": {"url": "about:blank"},
                        }))
                        _cdp.settimeout(4)
                        try: _cdp.recv()
                        except Exception: pass
                        import time as _time_mod; _time_mod.sleep(0.5)

                        _cdp.send(json.dumps({
                            "id": 102,
                            "method": "Page.navigate",
                            "params": {"url": _orig_url},
                        }))
                        _cdp.settimeout(6)
                        try: _cdp.recv()
                        except Exception: pass
                        logger.info(f"lifespan: cold-navigated UC Chrome → {_orig_url[:60]}")

                    _cdp.close()
                    logger.info(f"lifespan: stealth JS active in UC Chrome: {_t['url'][:60]}")
                except Exception as _ce:
                    logger.debug(f"lifespan: UC Chrome CDP inject skip: {_ce}")
    except Exception as _uce:
        logger.debug(f"lifespan: UC Chrome stealth inject (non-fatal): {_uce}")

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


def _get_proxied_public_ip(socks5h_port: int = 19055, timeout: int = 8) -> str | None:
    """Query public IP through the WS SOCKS5 bridge → PPPoE residential exit.

    Returns the residential IP that the browser actually exits from — NOT the
    Colab datacenter IP. This is what must be used for geo-resolve so that the
    stealth JS timezone/locale/lat/lon match the proxy exit, not the GCP datacenter.

    Uses socks5h (remote hostname resolution) so ipify.org domain is sent to the
    PPPoE proxy and resolved from the residential connection — identical to how
    Chrome sends traffic with --proxy-server=socks5://.

    Falls back to direct (Colab IP) only if the bridge is not reachable.
    IP changes on every Mac reboot — never cached, always live per call.
    """
    try:
        import socket as _s, struct as _struct

        def _socks5h_get(host: str, port: int, target_host: str, target_port: int, timeout: int) -> bytes:
            """Open a SOCKS5h connection through host:port to target_host:target_port."""
            sock = _s.create_connection((host, port), timeout=timeout)
            sock.settimeout(timeout)
            # Greeting: no auth
            sock.sendall(b"\x05\x01\x00")
            if sock.recv(2) != b"\x05\x00":
                raise ConnectionError("SOCKS5 auth failed")
            # CONNECT with ATYP=3 (domain) — remote hostname resolution
            host_b = target_host.encode()
            sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                         + _struct.pack("!H", target_port))
            rep = sock.recv(10)
            if len(rep) < 2 or rep[1] != 0:
                raise ConnectionError(f"SOCKS5 CONNECT failed: {rep!r}")
            # HTTP/1.0 GET
            sock.sendall(b"GET / HTTP/1.0\r\nHost: api.ipify.org\r\n\r\n")
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            sock.close()
            # Response body is after \r\n\r\n
            return data.split(b"\r\n\r\n", 1)[-1].strip()

        ip = _socks5h_get("127.0.0.1", socks5h_port, "api.ipify.org", 80, timeout)
        if ip and len(ip) < 40:
            return ip.decode()
    except Exception:
        pass

    # Fallback: direct (Colab datacenter IP) — geo will be wrong but better than None
    try:
        import urllib.request as _ur
        with _ur.urlopen("https://api.ipify.org", timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None


def _get_runtime_public_ip() -> str | None:
    """Detect this runtime's public IP via proxy (residential exit) if available,
    else direct. Prefer _get_proxied_public_ip() for all geo-resolve calls.
    Returns None on total failure — caller falls back to 'UTC'.
    """
    return _get_proxied_public_ip()




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
# Priority: 1) real google-chrome-stable (undetectable), 2) patchright
# Chromium from ms-patchright cache, 3) let patchright auto-detect.
_REAL_CHROME = "/usr/bin/google-chrome-stable"

import os as _os, glob as _glob
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
    _REAL_CHROME      if _os.path.isfile(_REAL_CHROME) else
    _PATCHRIGHT_CHROME if _PATCHRIGHT_CHROME and _os.path.isfile(_PATCHRIGHT_CHROME) else
    None  # let patchright auto-detect via PLAYWRIGHT_BROWSERS_PATH
)

# UA must EXACTLY match the installed Chrome binary version.
# Chrome 131.0.6778.108 = last version UC 3.5.5 fully patches (navigator.webdriver=False confirmed).
# Chrome 153 is NOT used — UC 3.5.5 doesn't patch it and Google gives only 3 cookies.
_CHROME_VERSION = "131.0.6778.108"  # /opt/chrome131/chrome
_STEALTH_UA_CHROME = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROME_VERSION} Safari/537.36"
)


# Chromium launch args (hardened, stealth, Colab-compatible)
CHROMIUM_ARGS = [
    "--no-sandbox",
    # NOTE: --disable-setuid-sandbox removed — shows a visible warning banner
    # ("unsupported command-line flag") which is a bot detection signal.
    # Running as root in Colab, --no-sandbox alone is sufficient.
    "--disable-dev-shm-usage",
    "--ignore-gpu-blocklist",
    # Anti-automation detection
    "--disable-blink-features=AutomationControlled",
    "--disable-automation",
    "--disable-infobars",
    "--remote-debugging-address=0.0.0.0",  # bind to Tailscale IP
    # Language
    "--lang=en-US,en",
    "--accept-lang=en-US,en;q=0.9,en-GB;q=0.8",
    # WebGL: use ANGLE over EGL/Mesa (not SwiftShader Vulkan).
    # SwiftShader-Vulkan is a known headless indicator — EGL-ANGLE is far more
    # realistic and still works on Xvfb with Mesa drivers.
    "--use-gl=angle",
    "--use-angle=gl",           # gl = EGL/Mesa (more realistic than swiftshader)
    "--enable-webgl",
    "--enable-webgl2",
    # Window geometry (800x600 default = instant bot flag)
    "--window-size=1920,1080",
    "--start-maximized",
    # Session stability
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    # Fix: Trusted Types blocks blob Worker() in headless-detector V4 check.
    # Disabling TrustedDOMTypes allows importScripts()-based Worker UA patching.
    "--disable-features=TrustedDOMTypes",
    # NOTE: do NOT add --disable-extensions-except= — it blocks stealth extension
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
  // ── 1. navigator.webdriver → delete from prototype (avoids webdriver_getter_modified signal) ──
  // Object.defineProperty on the *instance* leaves a detectable custom descriptor.
  // Deleting from Navigator.prototype means getOwnPropertyDescriptor(navigator,'webdriver')
  // returns undefined — indistinguishable from a browser that never had the property.
  try {
    delete Navigator.prototype.webdriver;
  } catch (_) {
    // Fallback: redefine on prototype (not instance) — descriptor still looks native
    try {
      Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: () => undefined, configurable: true, enumerable: false,
      });
    } catch (_2) {}
  }

  // ── 2. Remove CDP residual cdc_ keys ─────────────────────────────────────
  Object.getOwnPropertyNames(window).filter(k => k.match(/^cdc_/))
    .forEach(k => { try { delete window[k]; } catch (_) {} });

  // ── 2b. Worker UA spoof — intercept Worker() so Web Workers report the ────
  // same spoofed UA as the main thread. Skips module workers (importScripts()
  // is forbidden there) to avoid breaking Cloudflare Turnstile etc.
  try {
    const _SPOOF_UA = '{UA}';
    const _CORES    = {CORES};
    const _RAM      = {RAM};
    const _OW = window.Worker;
    window.Worker = function(url, opts) {
      // Never touch module workers — importScripts() throws in module context.
      if (opts && opts.type === 'module') return new _OW(url, opts);
      if (typeof url !== 'string') return new _OW(url, opts);
      try {
        const _patch = [
          'Object.defineProperty(self.navigator,"userAgent",{get:()=>',
          JSON.stringify(_SPOOF_UA), ',configurable:true,enumerable:true});',
          'Object.defineProperty(self.navigator,"platform",{get:()=>"Win32",configurable:true});',
          'Object.defineProperty(self.navigator,"appVersion",{get:()=>',
          JSON.stringify(_SPOOF_UA.replace('Mozilla/','')), ',configurable:true});',
          'Object.defineProperty(self.navigator,"vendor",{get:()=>"Google Inc.",configurable:true});',
          'Object.defineProperty(self.navigator,"hardwareConcurrency",{get:()=>',String(_CORES),',configurable:true});',
          'Object.defineProperty(self.navigator,"deviceMemory",{get:()=>',String(_RAM),',configurable:true});',
        ].join('');
        const _pBlob = new Blob([_patch], {type:'text/javascript'});
        const _pUrl  = URL.createObjectURL(_pBlob);
        const _chain = new Blob(
          ['importScripts('+JSON.stringify(_pUrl)+','+JSON.stringify(url)+');'],
          {type:'text/javascript'}
        );
        const _wUrl = URL.createObjectURL(_chain);
        const _w = new _OW(_wUrl, opts);
        return _w;
      } catch(_) { return new _OW(url, opts); }
    };
    window.Worker.prototype = _OW.prototype;
    Object.defineProperty(window.Worker,'toString',
      {value:()=>'function Worker() { [native code] }',configurable:true});
  } catch (_) {}


  // ── 3. navigator.plugins / mimeTypes / language / languages ─────────────
  // Chrome 131 made navigator.plugins non-configurable — Object.defineProperty
  // silently fails on both instance and prototype levels.
  // Solution: Replace window.navigator with a Proxy that intercepts specific props.
  // window.navigator IS configurable (not LegacyUnforgeable on Window).
  const _fakePlugins = [
    { name: 'Chrome PDF Plugin',          filename: 'internal-pdf-viewer',          description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer',          filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client',              filename: 'internal-nacl-plugin',          description: '' },
    { name: 'WebKit built-in PDF',        filename: 'internal-pdf-viewer',           description: '' },
    { name: 'Widevine Content Decryption Module', filename: 'widevinecdmadapter.dll', description: 'Enables Widevine licenses for play back of HTML audio/video content.' },
  ];
  const _buildFakePluginArr = () => {
    const arr = [];
    _fakePlugins.forEach((p, i) => {
      const plug = (typeof Plugin !== 'undefined')
        ? Object.assign(Object.create(Plugin.prototype), p)
        : { ...p };
      arr[i] = plug;
    });
    try { Object.defineProperty(arr, 'item',      { value: i => arr[i], configurable: true }); } catch(_){}
    try { Object.defineProperty(arr, 'namedItem', { value: n => arr.find(p => p.name === n), configurable: true }); } catch(_){}
    try { Object.defineProperty(arr, 'length',    { value: 5, writable: false, configurable: false, enumerable: false }); } catch(_){}
    if (typeof PluginArray !== 'undefined') { try { Object.setPrototypeOf(arr, PluginArray.prototype); } catch(_){} }
    return arr;
  };
  const _fakeMimeTypes = (() => {
    const mt = [
      { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
      { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' },
    ];
    try { Object.defineProperty(mt, 'length', { value: 2, writable: false }); } catch(_){}
    try { Object.defineProperty(mt, 'item',   { value: i => mt[i] }); } catch(_){}
    try { Object.defineProperty(mt, 'namedItem', { value: n => mt.find(m => m.type === n) }); } catch(_){}
    if (typeof MimeTypeArray !== 'undefined') { try { Object.setPrototypeOf(mt, MimeTypeArray.prototype); } catch(_){} }
    return mt;
  })();

  // ── Navigator Proxy — the nuclear option for non-configurable properties ──
  // Chrome 131 locked plugins/languages via [LegacyUnforgeable]-like IDL attrs.
  // We swap out window.navigator itself with a Proxy. This works because
  // window.navigator (on Window.prototype) IS configurable.
  try {
    const _realNav = window.navigator;
    const _navProxy = new Proxy(_realNav, {
      get(target, prop) {
        // ── Navigator property spoofs (all in one place) ──
        if (prop === 'plugins')              return _buildFakePluginArr();
        if (prop === 'mimeTypes')            return _fakeMimeTypes;
        if (prop === 'languages')            return [_LOCALE, _LANG];
        if (prop === 'language')             return _LOCALE;
        if (prop === 'userAgent')            return _UA;
        if (prop === 'appVersion')           return _UA.replace('Mozilla/', '');
        if (prop === 'platform')             return '{PLATFORM}';
        if (prop === 'hardwareConcurrency')  return {CORES};
        if (prop === 'deviceMemory')         return {RAM};
        if (prop === 'vendor')               return 'Google Inc.';
        if (prop === 'vendorSub')            return '';
        if (prop === 'productSub')           return '20030107';
        if (prop === 'maxTouchPoints')       return 0;
        if (prop === 'webdriver')            return false;
        if (prop === 'doNotTrack')           return null;
        // All other props: reflect from real navigator (bound to avoid 'this' issues)
        const val = Reflect.get(target, prop, target);
        return (typeof val === 'function') ? val.bind(target) : val;
      },
      has(target, prop) { return prop in target; },
      getOwnPropertyDescriptor(target, prop) {
        // Make proxy transparent to detection attempts
        return Object.getOwnPropertyDescriptor(target, prop);
      },
    });
    // Replace window.navigator with the proxy
    Object.defineProperty(window, 'navigator', {
      get: () => _navProxy,
      configurable: true, enumerable: true,
    });
  } catch (_) {
    // Fallback: prototype + instance patches if Proxy fails
    try { Object.defineProperty(Navigator.prototype, 'plugins',   { get: () => _buildFakePluginArr(), configurable: true }); } catch(_){}
    try { Object.defineProperty(Navigator.prototype, 'mimeTypes', { get: () => _fakeMimeTypes, configurable: true }); } catch(_){}
    try { Object.defineProperty(Navigator.prototype, 'languages', { get: () => [_LOCALE, _LANG], configurable: true }); } catch(_){}
    try { Object.defineProperty(navigator, 'plugins',   { get: () => _buildFakePluginArr(), configurable: true }); } catch(_){}
    try { Object.defineProperty(navigator, 'mimeTypes', { get: () => _fakeMimeTypes, configurable: true }); } catch(_){}
    try { Object.defineProperty(navigator, 'languages', { get: () => [_LOCALE, _LANG], configurable: true }); } catch(_){}
  }



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
  // Prototype-level patch → works even in restricted CDP/patchright contexts
  try { Object.defineProperty(Navigator.prototype, 'language',  { get: function() { return _LOCALE; }, configurable: true, enumerable: true }); } catch (_) {}
  try { Object.defineProperty(Navigator.prototype, 'languages', { get: function() { return [_LOCALE, _LANG]; }, configurable: true, enumerable: true }); } catch (_) {}
  // Instance-level backup (belt-and-suspenders)
  try { Object.defineProperty(navigator, 'language',  { get: () => _LOCALE, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => [_LOCALE, _LANG], configurable: true }); } catch (_) {}

  // ── 6. User-Agent + related navigator props ───────────────────────────────
  const _UA = '{UA}';
  // Prototype-level patches (primary — survive patchright context restrictions)
  const _def = (proto, key, val) => {
    try { Object.defineProperty(proto, key, { get: typeof val === 'function' ? val : () => val, configurable: true, enumerable: true }); } catch (_) {}
  };
  _def(Navigator.prototype, 'userAgent',          () => _UA);
  _def(Navigator.prototype, 'appVersion',         () => _UA.replace('Mozilla/', ''));
  _def(Navigator.prototype, 'hardwareConcurrency',() => {CORES});
  _def(Navigator.prototype, 'deviceMemory',       () => {RAM});
  _def(Navigator.prototype, 'platform',           () => '{PLATFORM}');
  _def(Navigator.prototype, 'vendor',             () => 'Google Inc.');
  _def(Navigator.prototype, 'vendorSub',          () => '');
  _def(Navigator.prototype, 'productSub',         () => '20030107');
  _def(Navigator.prototype, 'maxTouchPoints',     () => 0);
  _def(Navigator.prototype, 'cookieEnabled',      () => true);
  _def(Navigator.prototype, 'onLine',             () => true);
  _def(Navigator.prototype, 'doNotTrack',         () => null);
  // Instance-level fallback (some properties need own-property to shadow prototype)
  try { Object.defineProperty(navigator, 'userAgent',           { get: () => _UA, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'appVersion',          { get: () => _UA.replace('Mozilla/', ''), configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => {CORES}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'deviceMemory',        { get: () => {RAM}, configurable: true }); } catch (_) {}
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
  // Chrome 131: WebGLRenderingContext.prototype.getParameter is NON-WRITABLE.
  // Direct assignment (proto.getParameter = ...) silently fails in sloppy mode.
  // Solution: Intercept HTMLCanvasElement.prototype.getContext — it IS configurable.
  // Patch each context INSTANCE via Object.defineProperty (own props > prototype).
  const _WEBGL_VENDOR   = '{WEBGL_V}';
  const _WEBGL_RENDERER = '{WEBGL_R}';

  const _patchCtxInstance = (ctx) => {
    if (!ctx) return ctx;
    // Own-property override — takes priority over non-writable prototype method
    const _origGP  = ctx.getParameter.bind(ctx);
    const _origGE  = ctx.getExtension.bind(ctx);
    try {
      Object.defineProperty(ctx, 'getParameter', {
        value: function(p) {
          if (p === 37445) return _WEBGL_VENDOR;
          if (p === 37446) return _WEBGL_RENDERER;
          return _origGP(p);
        }, writable: true, configurable: true, enumerable: false,
      });
    } catch(_){}
    try {
      Object.defineProperty(ctx, 'getExtension', {
        value: function(name) {
          if (name === 'WEBGL_debug_renderer_info') {
            return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
          }
          return _origGE(name);
        }, writable: true, configurable: true, enumerable: false,
      });
    } catch(_){}
    return ctx;
  };

  // Intercept canvas.getContext at HTMLCanvasElement prototype level (IS configurable)
  try {
    const _origGetCtx = HTMLCanvasElement.prototype.getContext;
    Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
      value: function(type, ...args) {
        const ctx = _origGetCtx.apply(this, [type, ...args]);
        if (ctx && (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl')) {
          return _patchCtxInstance(ctx);
        }
        return ctx;
      }, writable: true, configurable: true, enumerable: false,
    });
  } catch(_){}

  // Belt-and-suspenders: also try prototype-level (may work in some contexts)
  const _patchWebGLProto = (proto) => {
    if (!proto) return;
    try {
      const _o = proto.getParameter;
      Object.defineProperty(proto, 'getParameter', {
        value: function(p) {
          if (p === 37445) return _WEBGL_VENDOR;
          if (p === 37446) return _WEBGL_RENDERER;
          return _o.call(this, p);
        }, writable: true, configurable: true, enumerable: false
      });
    } catch(_){}
    try {
      const _e = proto.getExtension;
      Object.defineProperty(proto, 'getExtension', {
        value: function(name) {
          if (name === 'WEBGL_debug_renderer_info')
            return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
          return _e.call(this, name);
        }, writable: true, configurable: true, enumerable: false
      });
    } catch(_){}
  };
  try { _patchWebGLProto(WebGLRenderingContext.prototype); } catch(_){}
  try { _patchWebGLProto(WebGL2RenderingContext.prototype); } catch(_){}


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
        "ssh_proxy_url":   _SSH_PROXY_URL,
        # Direct noVNC URL for HITL interaction (zero-relay, X11 stream)
        "novnc_url":       _NOVNC_URL,
    }


@app.get("/debug/stealth-js", response_class=PlainTextResponse)
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
        "Apple" if "Apple" in _webgl_r else
        "Google Inc. (NVIDIA)" if "NVIDIA" in _webgl_r else
        "Google Inc. (Intel)"
    )
    _tz_fb = _fp_fb.get("timezone", "America/New_York")
    try:
        import datetime as _dtfb; import zoneinfo as _zifb
        _tz_off_fb = -int(_dtfb.datetime.now(_zifb.ZoneInfo(_tz_fb)).utcoffset().total_seconds() // 60)
    except Exception:
        _tz_off_fb = 300
    _js_out = (
        _STEALTH_JS
        .replace("{WEBGL_V}",     _webgl_v)
        .replace("{WEBGL_R}",     _webgl_r)
        .replace("{UA}",          _fp_fb.get("ua_template", _STEALTH_UA_CHROME))
        .replace("{PLATFORM}",    _fp_fb.get("platform",    "Win32"))
        .replace("{TIMEZONE}",    _tz_fb)
        .replace("{TZ_OFFSET}",   str(_tz_off_fb))
        .replace("{CANVAS_SEED}", str(_fp_fb.get("canvas_seed", 42)))
        .replace("{AUDIO_SEED}",  str(_fp_fb.get("audio_seed",  7)))
        .replace("{SCREEN_W}",    str(_fp_fb.get("width",   1920)))
        .replace("{SCREEN_H}",    str(_fp_fb.get("height",  1080)))
        .replace("{CORES}",       str(_fp_fb.get("cores",   8)))
        .replace("{RAM}",         str(_fp_fb.get("ram",     16)))
        .replace("'{LOCALE}'",    "'en-US'")
        .replace("{LOCALE}",      "en-US")
        .replace("{LANG}",        "en-US")
        .replace("{CH_PLATFORM}", _fp_fb.get("ch_platform", "macOS"))
        .replace("{CH_ARCH}",     _fp_fb.get("ch_arch",     "arm"))
        .replace("{LAT}",         str(_fp_fb.get("lat",  40.7128)))
        .replace("{LON}",         str(_fp_fb.get("lon", -74.0060)))
        .replace("{IS_MOBILE}",   "false")
        .replace("{SCREEN_AH}",   str(_fp_fb.get("height", 1080)))
        .replace("{UA_TEMPLATE}",  _fp_fb.get("ua_template", _STEALTH_UA_CHROME))
    )
    return _js_out



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
            logger.info(f"launch: auto-loaded fingerprint for {_prfl_id}: "
                        f"ua={_fp.get('ua_template','')[:40]}...")

    _canvas_seed = _fp.get("canvas_seed", 0x1A2B)

    _audio_seed  = _fp.get("audio_seed",  0x3C4D)
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
        .replace("{CORES}",   _cores)
        .replace("{RAM}", _ram)
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
    if CHROME_EXECUTABLE:
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
        "stealth_js":   _session_stealth_js,   # cached for /debug/stealth-js audit endpoint
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
    m = re.match(r"socks5h?://([^:]+):(\d+)", proxy_url)  # handles socks5:// and socks5h://
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
              logger.info(f"uc-login: Chrome proxy → WS bridge socks5h://127.0.0.1:{_ws_port} (→ {proxy_url[:40]})")
          elif _port_open(_ssh_port):
              _local_bridge_port = _ssh_port
              logger.info(f"uc-login: Chrome proxy → SSH tunnel socks5h://127.0.0.1:{_ssh_port} (→ {proxy_url[:40]})")
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

          # Step 2: resolve actual exit IP through the proxy (dynamic — changes on reboot).
          # We do NOT abort on IP mismatch with exit_node_public_ip — PPPoE IPs rotate
          # on every Mac reboot. The actual residential IP (whatever slot is currently active)
          # is used for all geo data. expected_ip is advisory only.
          _expected_ip = exit_node_public_ip
          logger.info(f"uc-login: resolving actual exit IP through proxy (expected={_expected_ip})...")
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
              # PPPoE IP has rotated (Mac reboot, ISP reassignment) — use actual, log advisory
              logger.warning(
                  f"uc-login: PPPoE IP rotated — actual={_actual_ip} expected={_expected_ip}. "
                  "Proceeding with actual residential IP for geo-resolve (normal after Mac reboot)."
              )
          elif _actual_ip:
              logger.info(f"uc-login: ✅ Exit IP confirmed: {_actual_ip}")
          else:
              logger.warning("uc-login: Could not determine exit IP — proceeding with caution")

          # Step 3: resolve full geo profile for verified/actual IP
          # Always use the actual observed exit IP. If still unknown, fall back to expected.
          _geo_ip = _actual_ip or _expected_ip or ""
          _uc_geo = _resolve_ip_geo(_geo_ip)

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
      # --disable-setuid-sandbox removed — shows warning banner, --no-sandbox alone suffices as root
      opts.add_argument("--disable-dev-shm-usage")
      opts.add_argument("--no-zygote")         # required in Colab containers (no zygote process)
      # Software WebGL: use ANGLE over EGL/Mesa, NOT SwiftShader-Vulkan.
      # SwiftShader-Vulkan ("SwiftShader Device (Subzero)") is a known headless fingerprint.
      # ANGLE-gl uses Mesa EGL which reports a far less suspicious renderer string.
      opts.add_argument("--use-gl=angle")
      opts.add_argument("--use-angle=gl")       # EGL/Mesa, not swiftshader
      opts.add_argument("--enable-webgl")
      opts.add_argument("--enable-webgl2")
      opts.add_argument("--ignore-gpu-blocklist")
      opts.add_argument("--disable-blink-features=AutomationControlled")
      opts.add_argument("--disable-service-workers")
      opts.add_argument("--disable-features=ServiceWorker")
      opts.add_argument("--window-size=1920,1080")
      opts.add_argument("--window-position=0,0")
      # NOTE: Do NOT pass --display=:99 as a Chrome flag — it's not a valid Chrome argument.
      # DISPLAY is set correctly via env var in the subprocess env dict below.
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
      # ── Write stealth Chrome extension — guaranteed Main World document_start injection ──
      # CDP Page.addScriptToEvaluateOnNewDocument doesn't survive renderer process changes.
      # A Chrome extension with world:MAIN + run_at:document_start is the only reliable method.
      try:
          _ext_dir = "/tmp/xio_stealth_ext"
          os.makedirs(_ext_dir, exist_ok=True)
          # Build rendered stealth JS with PRFL fingerprint
          _ext_fp = fingerprint if isinstance(fingerprint, dict) else {}
          _ext_tz = _ext_fp.get("timezone", "America/New_York")
          try:
              import datetime as _dtx; import zoneinfo as _zix
              _ext_tz_off = -int(_dtx.datetime.now(_zix.ZoneInfo(_ext_tz)).utcoffset().total_seconds() // 60)
          except Exception: _ext_tz_off = 300
          _ext_wr = _ext_fp.get("webgl_renderer", "ANGLE (Apple, Apple M1, OpenGL 4.1)")
          _ext_wv = "Apple" if "Apple" in _ext_wr else "Google Inc. (NVIDIA)"
          _ext_js = (
              _STEALTH_JS
              .replace("{WEBGL_V}", _ext_wv).replace("{WEBGL_R}", _ext_wr)
              .replace("{UA}",          _ext_fp.get("ua_template", _STEALTH_UA_CHROME))
              .replace("{PLATFORM}",    _ext_fp.get("platform",    "Win32"))
              .replace("{TIMEZONE}",    _ext_tz)
              .replace("{TZ_OFFSET}",   str(_ext_tz_off))
              .replace("{CANVAS_SEED}", str(_ext_fp.get("canvas_seed", 42)))
              .replace("{AUDIO_SEED}",  str(_ext_fp.get("audio_seed",  7)))
              .replace("{SCREEN_W}",    str(_ext_fp.get("width",   1920)))
              .replace("{SCREEN_H}",    str(_ext_fp.get("height",  1080)))
              .replace("{CORES}",       str(_ext_fp.get("cores",   8)))
              .replace("{RAM}",         str(_ext_fp.get("ram",     16)))
              .replace("'{LOCALE}'",    "'en-US'")
              .replace("{LOCALE}",      "en-US")
              .replace("{LANG}",        "en-US")
              .replace("{CH_PLATFORM}", _ext_fp.get("ch_platform", "macOS"))
              .replace("{CH_ARCH}",     _ext_fp.get("ch_arch",     "arm"))
              .replace("{LAT}",         str(_ext_fp.get("lat",  40.7128)))
              .replace("{LON}",         str(_ext_fp.get("lon", -74.0060)))
              .replace("{IS_MOBILE}",   "false")
              .replace("{SCREEN_AH}",   str(_ext_fp.get("height", 1080)))
          )
          with open(f"{_ext_dir}/stealth.js", "w") as _ef:
              _ef.write(_ext_js)
          with open(f"{_ext_dir}/manifest.json", "w") as _mf:
              import json as _json_ext
              _json_ext.dump({
                  "manifest_version": 3,
                  "name": "XIO Stealth",
                  "version": "1.0",
                  "description": "XIOSYNC fingerprint spoofing",
                  "content_scripts": [{
                      "matches": ["<all_urls>"],
                      "js": ["stealth.js"],
                      "run_at": "document_start",
                      "all_frames": True,
                      "world": "MAIN",
                  }],
              }, _mf)
          opts.add_argument(f"--load-extension={_ext_dir}")
          logger.info(f"uc-login: stealth extension loaded from {_ext_dir}")
      except Exception as _ext_e:
          logger.warning(f"uc-login: stealth extension write failed (non-fatal): {_ext_e}")

      import socket as _sock
      _s = _sock.socket(); _s.bind(("", 0)); _uc_debug_port = _s.getsockname()[1]; _s.close()
      # NOTE: Do NOT add --remote-debugging-address=0.0.0.0 here.
      # UC internally adds --remote-debugging-host=127.0.0.1. In Chrome 131, having BOTH
      # --remote-debugging-host AND --remote-debugging-address causes Chrome to disable
      # remote debugging entirely — chromedriver then times out connecting (70s).
      # The port is also NOT added here: uc.Chrome(port=X) handles it internally.
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
                .replace("{CORES}",   _cores)
                .replace("{RAM}", _ram)
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
            # Success: sign-in page OR already-logged-in redirect (Gmail/Drive/etc.)
            _google_service = any(s in curr_start for s in [
                "accounts.google.com", "google.com/ServiceLogin",
                "mail.google.com", "drive.google.com", "docs.google.com",
                "myaccount.google.com", "youtube.com",
            ])
            if _google_service:
                _sl_ok = True
                break
            # Still on new-tab — force a second attempt
            logger.warning(f"uc-login: still on {curr_start[:60]} after attempt {_sl_attempt+1}, retrying")
            time.sleep(2.0)

        if not _sl_ok:
            # Last chance: try direct IP (bypass possible DNS issue through proxy)
            curr_start = driver.current_url
            # If we're already on a Google service page, that's fine — profile was pre-authed
            _google_service = any(s in curr_start for s in [
                "accounts.google.com", "google.com/ServiceLogin",
                "mail.google.com", "drive.google.com", "myaccount.google.com",
            ])
            if not _google_service and ("chrome://" in curr_start or "new-tab" in curr_start):
                raise Exception(
                    f"ServiceLogin unreachable after 3 attempts — "
                    f"still on {curr_start[:80]}. Check proxy connectivity."
                )
        logger.info(f"uc-login: page_url={curr_start}")

        # ── Pre-auth fast-path: profile already authenticated ─────────────────
        # If Chrome redirected to Gmail/Drive/myaccount instead of sign-in page,
        # the profile has a valid Google session — skip all login form steps.
        # IMPORTANT: strip query string first — otherwise 'mail.google.com' matches
        # 'accounts.google.com/v3/signin?continue=https://mail.google.com'
        _curr_path = curr_start.split("?")[0]
        _already_authed = any(s in _curr_path for s in [
            "mail.google.com", "drive.google.com", "docs.google.com",
            "myaccount.google.com", "youtube.com",
        ])
        if _already_authed:
            logger.info(f"uc-login: ✅ profile pre-authenticated → {curr_start[:80]} — skipping login form")
            # Flow continues to cookie extraction below

        # ── Handle accountchooser: Google shows saved accounts instead of email field ──
        # When the profile has a previous Google session, Google redirects to accountchooser.
        # We must click "Use another account" to get to the identifier (email) input.
        if not _already_authed and ("accountchooser" in curr_start or ("v3/signin" in curr_start and "accountchooser" in curr_start)):
            logger.info("uc-login: on accountchooser — clicking 'Use another account'")
            try:
                _clicked_other = False
                for _aci in range(10):   # up to 5s
                    try:
                        _res = driver.execute_script("""
                            var btns = document.querySelectorAll('[data-identifier]');
                            if (btns.length > 0) {
                                // Check if our target email is already in the chooser
                                var email = arguments[0];
                                for (var b of btns) {
                                    if (b.getAttribute('data-identifier') === email) {
                                        b.click();
                                        return 'clicked_account';
                                    }
                                }
                            }
                            // No matching account — click "Use another account"
                            var others = document.querySelectorAll('[data-action="use_another_account"]');
                            if (others.length > 0) { others[0].click(); return 'use_another'; }
                            var links = Array.from(document.querySelectorAll('li, [role="listitem"]'));
                            for (var l of links) {
                                if (l.textContent.includes('Use another account') || l.textContent.includes('another account')) {
                                    l.click(); return 'text_click';
                                }
                            }
                            return 'not_found';
                        """, email)
                        logger.info(f"uc-login: accountchooser JS result={_res}")
                        if _res in ('clicked_account', 'use_another', 'text_click'):
                            _clicked_other = True
                            time.sleep(2.5)
                            curr_start = driver.current_url
                            logger.info(f"uc-login: after chooser nav → {curr_start[:80]}")
                            break
                    except Exception as _ace:
                        logger.debug(f"uc-login: accountchooser JS attempt {_aci}: {_ace}")
                    time.sleep(0.5)
                if not _clicked_other:
                    logger.warning("uc-login: could not handle accountchooser — proceeding anyway")
            except Exception as _ac_err:
                logger.warning(f"uc-login: accountchooser handler error: {_ac_err}")

        if not _already_authed:
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
                        logger.warning(f"uc-login: recaptcha_solver.py not found at {_rc_path} — triggering HITL")
                        # ── reCAPTCHA HITL: pause login and hand off to operator ──
                        try:
                            import uuid as _rc_uuid
                            _xv_url = (
                                f"{XIOSYNC_BASE}/api/v1/xioview/sessions/prfl002-login/view"
                                if XIOSYNC_BASE else "XIOVIEW: prfl002-login"
                            )
                            _novnc_hint = f"\n⚡ noVNC (direct/low-lag): {_NOVNC_URL}" if _NOVNC_URL else ""
                            _rc_notice = HITLNotice(
                                organization_id=_rc_uuid.UUID("00000000-0000-7000-8000-000000000000"),
                                session_id="prfl002-login",
                                challenge_type="recaptcha",
                                message=(
                                    f"🔒 reCAPTCHA challenge for {email}\n"
                                    f"👁 Watch & solve live: {_xv_url}{_novnc_hint}\n"
                                    f"URL: {curr2[:120]}\n"
                                    "Click 'I'm not a robot' checkbox or 'Try another way' in XIOVIEW, then Resume."
                                ),
                                instructions=(
                                    f"Open {_xv_url} — solve the reCAPTCHA or choose another verification method. "
                                    f"noVNC (direct, low-lag): {_NOVNC_URL or 'N/A'}. "
                                    "Then POST /hitl/{id}/resume to continue."
                                ),
                            )
                            _hitl_store.create(_rc_notice)
                            _rc_notice_id = str(_rc_notice.id)
                            logger.info(
                                f"uc-login: ✅ reCAPTCHA HITL created id={_rc_notice_id} — "
                                f"view={_xv_url} — blocking 300s for operator"
                            )
                            _rc_event = _hitl_store._events.get(_rc_notice.id)
                            if _rc_event:
                                _rc_event.wait(timeout=300)
                            _rc_resumed = _hitl_store._notices.get(_rc_notice.id)
                            if _rc_resumed and _rc_resumed.state == HITLState.RESUMED:
                                logger.info("uc-login: reCAPTCHA HITL resumed — re-evaluating page")
                                curr2 = driver.current_url
                                _is_challenge = "challenge/" in curr2 and "challenge/pwd" not in curr2
                                if not _is_challenge:
                                    logger.info("uc-login: ✅ reCAPTCHA cleared after HITL")
                            else:
                                logger.warning("uc-login: reCAPTCHA HITL timed out — proceeding anyway")
                        except Exception as _rc_hitl_e:
                            logger.warning(f"uc-login: reCAPTCHA HITL error: {_rc_hitl_e}")
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
        # Quit the login Chrome to free RAM — cookies already captured above.
        # Colab has only 12.7GB RAM; two Chrome instances cause OOM kills.
        try:
            driver.quit()
            logger.info("uc-login: Chrome quit (RAM freed)")
        except Exception as _qe:
            logger.debug(f"uc-login: Chrome quit error (non-fatal): {_qe}")

        logger.info(f"uc-login: success cookies={len(cookies)} final_url={final_url}")
        return {
            "ok": True, "cookies": cookies, "engine": "uc",
            "final_url":   final_url,
            "profile_dir": user_data_dir,
            "uc_port":     0,   # Chrome is quit — no CDP port to expose
            "canvas_seed": _uc_canvas_seed if '_uc_canvas_seed' in locals() else 0,
            "audio_seed":  _uc_audio_seed if '_uc_audio_seed' in locals() else 0,
            "webgl_seed":  0,
            "ua_string":   _ua_str if '_ua_str' in locals() else "",
            "timezone":    _uc_timezone if '_uc_timezone' in locals() else "UTC",
            "locale":      _uc_locale if '_uc_locale' in locals() else "en-US",
        }

    except Exception as exc:
        logger.error(f"uc-login exception: {exc}")
        try:
            driver.quit()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    # NOTE: no finally quit — on success the driver is kept in _uc_drivers


# ── CDP Port Exposure (Tailscale-accessible) ──────────────────────────────────
# Chrome remote-debugging binds to 127.0.0.1 only (chromedriver security default).
# These endpoints expose CDP via port 9300 so XIOSYNC can connect from Mac Mini.

_cdp_forwarders: dict[str, asyncio.Task] = {}  # session_id → running forwarder task
_cdp_expose_ports: dict[str, int] = {}         # session_id → public port


class CDPExposeRequest(BaseModel):
    session_id: str
    local_port:  int           # Chrome CDP port on 127.0.0.1
    public_port: int = 0       # 0 = auto-pick a free port


@app.post("/cdp-expose")
async def cdp_expose(req: CDPExposeRequest) -> dict:
    """Start a TCP forwarder: Tailscale IP:public_port → 127.0.0.1:local_port.

    This makes the UC Chrome CDP port accessible to the XIOSYNC Mac Mini via
    Tailscale. Call after run-uc-login to get a Tailscale-accessible cdp_http_url.

    Returns the public_port to use in cdp_ws_url:
        ws://<tailscale_ip>:<public_port>
    """
    import socket as _sock

    sid = req.session_id
    local_port = req.local_port
    public_port = req.public_port

    # Cancel any existing forwarder for this session
    old_task = _cdp_forwarders.pop(sid, None)
    if old_task:
        old_task.cancel()

    # Auto-pick a free port if not specified
    if public_port == 0:
        s = _sock.socket()
        s.bind(("", 0))
        public_port = s.getsockname()[1]
        s.close()

    tailscale_ip = _my_tailscale_ip()

    async def _forward_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", local_port)
        except Exception:
            writer.close()
            return

        async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                try:
                    w.close()
                except Exception:
                    pass

        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer), return_exceptions=True)

    async def _run_forwarder() -> None:
        try:
            srv = await asyncio.start_server(
                _forward_client, host="0.0.0.0", port=public_port
            )
            logger.info(f"cdp-expose: {tailscale_ip}:{public_port} → 127.0.0.1:{local_port}")
            async with srv:
                await srv.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"cdp-expose forwarder died: {exc}")

    task = asyncio.ensure_future(_run_forwarder())
    _cdp_forwarders[sid] = task
    _cdp_expose_ports[sid] = public_port

    # Brief pause to let the server bind
    await asyncio.sleep(0.5)

    return {
        "ok": True,
        "session_id": sid,
        "tailscale_ip": tailscale_ip,
        "public_port": public_port,
        "local_port": local_port,
        "cdp_http_url": f"http://{tailscale_ip}:{public_port}",
        "cdp_ws_url":   f"ws://{tailscale_ip}:{public_port}",
    }


class CDPProxyRequest(BaseModel):
    session_id: str | None = None


@app.websocket("/cdp-ws-proxy/{session_id}")
async def cdp_ws_proxy(websocket, session_id: str) -> None:
    """Transparent WebSocket proxy: XIOSYNC ↔ local Chrome CDP.

    Allows patchright running on the Mac Mini to attach to UC Chrome on the
    Colab worker without requiring the Chrome CDP port to be publicly bound.
    patchright calls:
        pw.chromium.connect_over_cdp("http://100.111.130.118:9300/cdp-proxy/{session_id}")
    NOT YET: this WS endpoint proxies raw CDP frames bidirectionally.
    """
    from fastapi import WebSocket as _WS
    import websockets as _wsl  # noqa: PLC0415

    sess = _sessions.get(session_id, {})
    local_port = sess.get("port")
    if not local_port:
        await websocket.close(code=4404, reason="session_not_found")
        return

    # Get the actual page WebSocket URL from Chrome
    try:
        import urllib.request as _ulr
        import json as _json
        targets = _json.loads(_ulr.urlopen(f"http://127.0.0.1:{local_port}/json", timeout=5).read())
        page_ws_url = next(
            (t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page"), None
        )
        if not page_ws_url:
            await websocket.close(code=4404, reason="no_page_target")
            return
    except Exception as exc:
        await websocket.close(code=4500, reason=str(exc))
        return

    await websocket.accept()

    try:
        async with _wsl.connect(page_ws_url) as chrome_ws:
            async def _to_chrome():
                async for msg in websocket.iter_text():
                    await chrome_ws.send(msg)

            async def _from_chrome():
                async for msg in chrome_ws:
                    await websocket.send_text(msg)

            await asyncio.gather(_to_chrome(), _from_chrome(), return_exceptions=True)
    except Exception:
        pass
    finally:
        await websocket.close()


@app.get("/cdp-proxy/{session_id}/json/version")
async def cdp_proxy_version(session_id: str) -> dict:
    """Return Chrome version info — makes this URL look like a real CDP HTTP endpoint."""
    sess = _sessions.get(session_id, {})
    local_port = sess.get("port")
    if not local_port:
        from fastapi import HTTPException as _HTTPExc  # noqa: PLC0415
        raise _HTTPExc(404, detail="session_not_found")
    import urllib.request as _ulr
    import json as _json
    try:
        data = _json.loads(_ulr.urlopen(f"http://127.0.0.1:{local_port}/json/version", timeout=5).read())
        # Override webSocketDebuggerUrl to point through this proxy
        tailscale_ip = _my_tailscale_ip()
        data["webSocketDebuggerUrl"] = f"ws://{tailscale_ip}:9300/cdp-ws-proxy/{session_id}"
        return data
    except Exception as exc:
        from fastapi import HTTPException as _HTTPExc  # noqa: PLC0415
        raise _HTTPExc(500, detail=str(exc)) from exc


def _launch_uc_chrome_for_login(profile_dir: str, proxy_url: str | None) -> tuple:
    """
    Launch UC Chrome (Chrome 131) for login — returns (driver, port).
    Module-level so /run-uc-login can call it directly (no subprocess).
    Uses use_subprocess=False — same config as the working lifespan Chrome.
    All traffic routes through proxy_url; Colab IP is never exposed.
    """
    import undetected_chromedriver as uc
    import socket as _sock

    # Prefer Chrome 131 (best UC 3.5.5 compatibility)
    _chrome_bin, _chrome_ver = "/opt/chrome131/chrome", 131
    if not (os.path.isfile(_chrome_bin) and os.access(_chrome_bin, os.X_OK)):
        for _p in ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"]:
            if os.path.isfile(_p) and os.access(_p, os.X_OK):
                try:
                    import subprocess as _sp2
                    _raw = _sp2.check_output([_p, "--version"], timeout=5, stderr=_sp2.DEVNULL).decode()
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
    opts.add_experimental_option("prefs", {
        "webrtc.ip_handling_policy":     "disable_non_proxied_udp",
        "webrtc.multiple_routes_enabled": False,
        "webrtc.nonproxied_udp_enabled":  False,
    })

    _s = _sock.socket(); _s.bind(("", 0)); port = _s.getsockname()[1]; _s.close()

    # Set up chromedriver binary
    _our_cd = f"/usr/local/bin/chromedriver{_chrome_ver}"
    if not os.path.isfile(_our_cd):
        _our_cd = "/usr/local/bin/chromedriver131"
    _uc_cd_dir = os.path.expanduser("~/.local/share/undetected_chromedriver")
    _uc_cd_path = os.path.join(_uc_cd_dir, "undetected_chromedriver")
    if os.path.isfile(_our_cd):
        os.makedirs(_uc_cd_dir, exist_ok=True)
        import shutil as _sh2
        _sh2.copy2(_our_cd, _uc_cd_path)
        os.chmod(_uc_cd_path, 0o755)

    # Clean stale profile locks
    os.makedirs(profile_dir, exist_ok=True)
    for _lk in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try: os.remove(os.path.join(profile_dir, _lk))
        except FileNotFoundError: pass

    logger.info(f"_launch_uc_chrome_for_login: Chrome {_chrome_ver} proxy={bool(proxy_url)} port={port}")
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
        # ── IN-PROCESS APPROACH (replaces subprocess runner) ──────────────────────
        # Root cause of subprocess failure: Chrome inherits the agent's open file
        # descriptors (patchright websockets) → cannot bind CDP port.
        # SOCKS5 proxy gets saturated when a new Chrome is spawned alongside Chrome 131.
        #
        # Fix: use _launch_uc_chrome() (same as /run-uc-login-start + lifespan Chrome)
        # which runs Chrome 131 in-process via run_in_executor with use_subprocess=False.
        # Then run the login directly via _run_uc_login_sync(pre_launched_driver=driver).
        # This is the same working 2-step flow, collapsed into a single atomic call.
        # ──────────────────────────────────────────────────────────────────────────
        loop = asyncio.get_event_loop()
        # ── CRITICAL: use a FRESH per-email profile for the login Chrome ──────────
        # The cascade-pulled _profile_dir belongs to a reference account (PRFL-002)
        # and has live cookies → Chrome auto-redirects to Gmail, never shows sign-in form.
        # Always use an isolated clean profile so Chrome shows the email/password form.
        _email_slug = re.sub(r"[^a-zA-Z0-9]", "_", req.email.split("@")[0])[:24]
        _login_profile_dir = f"/tmp/uc_login_{_email_slug}"
        logger.info(f"uc-login: using fresh login profile: {_login_profile_dir}")

        logger.info("uc-login: launching Chrome in-process via _launch_uc_chrome_for_login()...")
        _uc_drv, _uc_port = await loop.run_in_executor(
            None, lambda: _launch_uc_chrome_for_login(_login_profile_dir, proxy_url)
        )
        logger.info(f"uc-login: Chrome ready on port {_uc_port}, running login in executor...")

        result = await loop.run_in_executor(
            None,
            lambda: _run_uc_login_sync(
                email=req.email,
                password=req.password,
                totp_secret=req.totp_secret or "",
                proxy_url=proxy_url,
                user_agent=user_agent,
                user_data_dir=_login_profile_dir,   # fresh profile — not cascade PRFL
                pre_launched_driver=_uc_drv,
                exit_node_public_ip=req.exit_node_public_ip or None,
                fingerprint=req.fingerprint if hasattr(req, "fingerprint") else None,
            ),
        )
        logger.info(f"uc-login: in-process result: ok={result.get('ok')} url={result.get('url','?')}")
        # Attach port so cookie injection downstream knows where Chrome lives
        if "uc_port" not in result:
            result["uc_port"] = _uc_port

        if result.get("ok") and result.get("uc_port"):
            # ── Register session so /health and other endpoints can reference it ──
            _uc_port = result["uc_port"]
            _tailscale_ip = _my_tailscale_ip()
            _sessions[req.session_id] = {
                "browser":     None,   # UC Chrome — Selenium manages it, not patchright
                "context":     None,
                "pw":          None,
                "pid":         0,
                "port":        _uc_port,
                "cdp_ws_url":  f"ws://{_tailscale_ip}:{_uc_port}",  # placeholder
                "proxy_url":   proxy_url,
                "profile_dir": _profile_dir,
                "chrome_proc": None,
                "guard_task":  None,
                "timezone":    _tz,
                "exit_node_ip": _exit_ip,
            }

            # ── Auto-expose: make Chrome CDP accessible on Tailscale via TCP forwarder ──
            # UC Chrome binds remote-debugging to 127.0.0.1 only (chromedriver security
            # default). Use our asyncio TCP forwarder to punch it through to Tailscale.
            try:
                _expose_req = CDPExposeRequest(session_id=req.session_id, local_port=_uc_port)
                _expose_result = await cdp_expose(_expose_req)
                _cdp_ws = _expose_result["cdp_ws_url"]
                _sessions[req.session_id]["cdp_ws_url"] = _cdp_ws
                logger.info(
                    f"run-uc-login: CDP exposed session={req.session_id} "
                    f"tailscale={_expose_result['cdp_http_url']}"
                )
            except Exception as _expose_exc:
                _cdp_ws = f"ws://{_tailscale_ip}:{_uc_port}"
                logger.warning(f"run-uc-login: cdp-expose failed: {_expose_exc} — using direct URL")

            result["cdp_ws_url"] = _cdp_ws
            result["cdp_http_url"] = _cdp_ws.replace("ws://", "http://")
            result["session_id"] = req.session_id          # fix: mjs reads ucResult.session_id
            result["profile_dir"] = _login_profile_dir     # fix: mjs reads ucResult.profile_dir
            logger.info(
                f"run-uc-login: session registered session={req.session_id} port={_uc_port} "
                f"cdp_ws={_cdp_ws}"
            )

            # Write fingerprint JSON for this profile
            try:
                import json as _json, datetime as _dt
                _prfl_id = req.identity_id.replace("-", "")[:16] if (hasattr(req, "identity_id") and req.identity_id) else req.email.split("@")[0]
                _fp_data = {
                    "profile_id": _prfl_id,
                    "email": req.email,
                    "canvas_seed": result.get("canvas_seed", 0),
                    "audio_seed": result.get("audio_seed", 0),
                    "webgl_seed": result.get("webgl_seed", 0),
                    "ua": result.get("ua_string", ""),
                    "timezone": result.get("timezone", "UTC"),
                    "locale": result.get("locale", "en-US"),
                    "created_at": _dt.datetime.now(datetime.UTC).isoformat(),
                }
                _fp_path = f"/content/drive/MyDrive/XIOSYNC-Shared/profiles/PRFL_{_prfl_id}.fingerprint.json"
                os.makedirs(os.path.dirname(_fp_path), exist_ok=True)
                with open(_fp_path, 'w') as _ff:
                    _json.dump(_fp_data, _ff, indent=2)
                logger.info(f"run-uc-login: fingerprint saved → {_fp_path}")
            except Exception as _fe:
                logger.warning(f"run-uc-login: fingerprint save failed: {_fe}")


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
                    import uuid as _hitl_uuid
                    _org_id = _hitl_uuid.UUID("00000000-0000-7000-8000-000000000000")
                    _xioview_url = (
                        f"{XIOSYNC_BASE}/api/v1/xioview/sessions/prfl002-login/view"
                        if XIOSYNC_BASE else "XIOVIEW: prfl002-login"
                    )
                    _notice = HITLNotice(
                        organization_id=_org_id,
                        session_id="prfl002-login",
                        challenge_type="recaptcha" if "captcha" in _error_msg.lower() or "rejected" in _error_msg.lower() else "login_failed",
                        message=(
                            f"Google sign-in challenge for {req.email}.\n"
                            f"Error: {_error_msg[:200]}\n"
                            f"👁 Watch live: {_xioview_url}\n"
                            f"Action: Solve reCAPTCHA or verify account manually in the XIOVIEW browser, then Resume."
                        ),
                        identity_id=_hitl_uuid.UUID(req.identity_id) if req.identity_id else None,
                        instructions=(
                            "Open XIOVIEW URL above. Solve the reCAPTCHA or complete the verification. "
                            "Then POST /hitl/{id}/resume with {\"action\": \"resume\"} to continue login."
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
                    result["hitl_resume_url"] = f"http://127.0.0.1:{os.environ.get('XIORUN_AGENT_PORT', '9300')}/hitl/{_notice_id}/resume"

                    # ── Block and wait for operator to resume (up to 300s) ────
                    # The driver is still alive — XIOVIEW can control it.
                    # When operator resumes, re-check if login succeeded.
                    import asyncio as _hitl_asyncio
                    _hitl_event = _hitl_store._events.get(_notice.id)
                    if _hitl_event:
                        logger.info("run-uc-login: pausing — waiting for HITL resume...")
                        try:
                            await _hitl_asyncio.wait_for(_hitl_event.wait(), timeout=300.0)
                        except _hitl_asyncio.TimeoutError:
                            pass

                        _resumed_notice = _hitl_store._notices.get(_notice.id)
                        if _resumed_notice and _resumed_notice.state == HITLState.RESUMED:
                            logger.info("run-uc-login: HITL resumed by operator — re-checking login state")
                            try:
                                _post_hitl_url = driver.current_url
                                logger.info(f"run-uc-login: post-HITL URL: {_post_hitl_url[:100]}")
                                if "myaccount.google.com" in _post_hitl_url or "mail.google.com" in _post_hitl_url:
                                    result["ok"] = True
                                    result["error"] = ""
                                    result["hitl_state"] = "RESUMED_SUCCESS"
                                    logger.info("run-uc-login: ✅ Login succeeded after HITL resume!")
                                elif "accounts.google.com" not in _post_hitl_url:
                                    result["ok"] = True
                                    result["hitl_state"] = "RESUMED_SUCCESS"
                                    logger.info(f"run-uc-login: ✅ Navigated away from Google login after HITL: {_post_hitl_url[:80]}")
                            except Exception as _phr:
                                logger.warning(f"run-uc-login: post-HITL URL check error: {_phr}")
                        else:
                            logger.warning("run-uc-login: HITL timed out (300s) — no operator action")
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
        # --disable-setuid-sandbox removed — triggers visible warning banner (bot signal)
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--use-gl=angle")
        opts.add_argument("--use-angle=gl")       # EGL/Mesa, not swiftshader
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
        opts.add_experimental_option("prefs", {
            "webrtc.ip_handling_policy":     "disable_non_proxied_udp",
            "webrtc.multiple_routes_enabled": False,
            "webrtc.nonproxied_udp_enabled":  False,
            # NOTE: Do NOT add profile.exited_cleanly=True — clears session cookies
        })

        # Pick free debug port — passed to uc.Chrome(port=) directly (not via opts)
        # DO NOT also add --remote-debugging-port to opts; uc.Chrome sets it via port=
        _s = _sock.socket(); _s.bind(("", 0)); port = _s.getsockname()[1]; _s.close()

        # Allow XIOVIEW server (Mac Mini) to connect CDP WS without origin rejection
        opts.add_argument("--remote-allow-origins=*")
        # NOTE: Do NOT add --remote-debugging-address=0.0.0.0 — conflicts with UC's
        # internal --remote-debugging-host=127.0.0.1, disabling Chrome remote debugging.

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

        # Level 2: Live verify — launch patchright with persistent context, navigate to myaccount
        try:
            from patchright.async_api import async_playwright
            async with async_playwright() as pw:
                # MUST use launch_persistent_context for a user-data-dir profile.
                # browser.new_context(user_data_dir=...) is NOT valid in patchright.
                context = await pw.chromium.launch_persistent_context(
                    _profile_dir,
                    headless=True,
                    args=[
                        "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                    viewport={"width": 1440, "height": 900},
                    no_viewport=False,
                )
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://myaccount.google.com/", wait_until="networkidle", timeout=20000)
                await asyncio.sleep(1.5)

                prefix = req.email.split("@")[0].lower()
                body_text = await page.evaluate("document.body.innerText.toLowerCase()")
                is_valid = prefix in body_text and "sign in" not in (await page.title()).lower()

                await context.close()

                if is_valid:
                    logger.info(f"cascade-check: L2 live verify PASSED for {req.email}")
                    return {"valid": True, "profile_dir": _profile_dir, "level": "LIVE_VERIFY"}
                else:
                    logger.info(f"cascade-check: L2 live verify FAILED for {req.email}")
        except Exception as e:
            logger.warning(f"cascade-check: L2 verify error: {e}")

    # Level 3: Pull from Drive FUSE — canonical path is profiles/PRFL-NNN.tar.gz
    # Also check legacy chrome_profiles/PRFL_{hex}.tar.gz as a fallback.
    try:
        import tarfile as _tf_cc
        _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_id_short}__{_node_slug}"
        os.makedirs(_profile_dir, exist_ok=True)

        # Canonical: scan profiles/PRFL-*.tar.gz and match by email via fingerprint JSON.
        # Strategy: for each PRFL-NNN.tar.gz, check if PRFL-NNN.fingerprint.json exists
        # and its 'email' field matches req.email. Pick the matching one; fall back to
        # the most-recent (highest serial) if no fingerprint match is found.
        _profiles_dir = os.path.join(DRIVE_ROOT, "profiles")
        _found_tar = None
        _fallback_tar = None
        if os.path.isdir(_profiles_dir):
            _candidates = sorted(
                [_p for _p in os.listdir(_profiles_dir) if _p.endswith(".tar.gz") and _p.startswith("PRFL-")],
                reverse=True,  # highest serial first (PRFL-003 before PRFL-002)
            )
            for _p in _candidates:
                _tar_path = os.path.join(_profiles_dir, _p)
                _prfl_stem = _p[: -len(".tar.gz")]          # e.g. "PRFL-003"
                _fp_path = os.path.join(_profiles_dir, f"{_prfl_stem}.fingerprint.json")
                if os.path.isfile(_fp_path):
                    try:
                        with open(_fp_path) as _fp_f:
                            _fp_data = json.load(_fp_f)
                        if _fp_data.get("email", "").lower() == req.email.lower():
                            _found_tar = _tar_path
                            logger.info(f"cascade-check: L3 fingerprint match {_prfl_stem} → {req.email}")
                            break
                    except Exception:
                        pass
                if _fallback_tar is None:
                    _fallback_tar = _tar_path  # highest-serial tar as fallback

            if not _found_tar and _fallback_tar:
                _found_tar = _fallback_tar
                logger.info(f"cascade-check: L3 no fingerprint match — using fallback {os.path.basename(_found_tar)}")

        # Legacy fallback: chrome_profiles/PRFL_{id_short}.tar.gz
        if not _found_tar:
            _legacy = os.path.join(DRIVE_ROOT, f"chrome_profiles/PRFL_{_id_short}.tar.gz")
            if os.path.exists(_legacy):
                _found_tar = _legacy

        if _found_tar:
            logger.info(f"cascade-check: L3 Drive profile found: {_found_tar}")
            try:
                with _tf_cc.open(_found_tar, "r:gz") as tf:
                    tf.extractall(path="/tmp/xiorun_profiles")
                logger.info(f"cascade-check: L3 profile extracted to {_profile_dir}")
                return {"valid": False, "profile_dir": _profile_dir, "level": "DRIVE_PULL",
                        "needs_verify": True, "source_tar": _found_tar}
            except Exception as _tex:
                logger.warning(f"cascade-check: L3 extract failed: {_tex}")
        else:
            logger.info(f"cascade-check: L3 no Drive profile found in profiles/ or chrome_profiles/")
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

    # 2. Save Chrome profile tar to Drive FUSE — canonical path: profiles/PRFL-NNN.tar.gz
    if req.profile_dir and os.path.isdir(req.profile_dir):
        try:
            import tarfile as _tf_ps, hashlib as _hl_ps
            _id_short = req.identity_id.replace("-", "")[:16]

            # Determine canonical profile number: scan existing profiles/ for one
            # already named for this identity, or allocate the next PRFL-NNN slot.
            _profiles_dir_ps = os.path.join(DRIVE_ROOT, "profiles")
            os.makedirs(_profiles_dir_ps, exist_ok=True)
            _existing_nums = []
            _matched_key = None
            if os.path.isdir(_profiles_dir_ps):
                for _pf in sorted(os.listdir(_profiles_dir_ps)):
                    if _pf.endswith(".tar.gz") and _pf.startswith("PRFL-"):
                        try:
                            _existing_nums.append(int(_pf[5:8]))
                        except ValueError:
                            pass

            # Check if a chrome_profiles/ entry already exists for backwards compat
            _legacy_key = f"chrome_profiles/PRFL_{_id_short}.tar.gz"
            _legacy_path = os.path.join(DRIVE_ROOT, _legacy_key)

            # Allocate new serial number if no existing canonical entry found
            _next_num = (max(_existing_nums) + 1) if _existing_nums else 1
            _profile_key = f"profiles/PRFL-{_next_num:03d}.tar.gz"
            _drive_path = os.path.join(DRIVE_ROOT, _profile_key)

            # Trim cache dirs before archiving
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
                # Atomic copy to canonical location
                import shutil as _sh2
                _sh2.copy2(_tmp_tar_path, _drive_path)
                results["profile_saved"] = True
                results["profile_key"] = _profile_key
                results["profile_size"] = _sz
                logger.info(f"persist-session: profile saved to Drive: {_profile_key} ({_sz:,}b)")

                # Clean up legacy chrome_profiles/ duplicate if it exists
                if os.path.exists(_legacy_path):
                    try:
                        os.unlink(_legacy_path)
                        logger.info(f"persist-session: removed legacy duplicate {_legacy_key}")
                    except Exception as _le:
                        logger.warning(f"persist-session: legacy cleanup failed: {_le}")
            finally:
                os.unlink(_tmp_tar_path)
        except Exception as e:
            logger.warning(f"persist-session: profile save error: {e}")

    return results


# ── AI Generation Endpoints ────────────────────────────────────────────────────

# Constants for native agy
_AGY_BIN = os.environ.get("XIOAI_AGY_BIN") or os.path.expanduser("~/.local/bin/agy")
_AGY_GEMINI_DIR = os.path.expanduser("~/.gemini")
_AGY_STATE_DIR = os.path.join(_AGY_GEMINI_DIR, "antigravity-cli")
_AGY_TOKEN_FILE = os.path.join(_AGY_GEMINI_DIR, "jetski-standalone-oauth-token")


# ── Per-profile fingerprint persistence ───────────────────────────────────────
# Maps each Chrome profile (PRFL-001 etc.) to a stable device identity stored
# alongside its .tar.gz on Drive as PRFL-NNN.fingerprint.json.
# Keys match exactly what the existing launch_browser() machinery expects.
#
# Only the STATIC identity fields are persisted (canvas/audio seeds, UA, screen,
# hardware). Runtime-dynamic fields (timezone, locale, lat/lon) are resolved
# live from the exit-node IP by the existing _resolve_ip_geo() — same as always.

_PROFILE_FINGERPRINT_CACHE: dict[str, dict] = {}

# Deterministic base presets per profile number — each profile = distinct persona
_PRFL_PRESETS: dict[int, dict] = {
    1: {  # PRFL-001: Indian professional, Windows 10 laptop, GTX 1050
        "canvas_seed": 0x1A2B3C, "audio_seed": 0x4D5E6F,
        "ua_template": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.6778.108 Safari/537.36"),
        "platform": "Win32", "ch_platform": "Windows", "ch_arch": "x86",
        "width": 1920, "height": 1080, "cores": 8, "ram": 8, "is_mobile": False,
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    2: {  # PRFL-002: Indian student, macOS Sequoia, M1 MacBook Air
        "canvas_seed": 0x7A8B9C, "audio_seed": 0xABCDEF,
        "ua_template": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"),
        "platform": "MacIntel", "ch_platform": "macOS", "ch_arch": "arm",
        "width": 2560, "height": 1600, "cores": 10, "ram": 16, "is_mobile": False,
        "webgl_renderer": "ANGLE (Apple, Apple M1, OpenGL 4.1)",
    },
    5: {  # PRFL-005: Developer, Linux Mint, Intel UHD
        "canvas_seed": 0xC0FFEE, "audio_seed": 0xDEADBE,
        "ua_template": ("Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/129.0.0.0 Safari/537.36"),
        "platform": "Linux x86_64", "ch_platform": "Linux", "ch_arch": "x86",
        "width": 1920, "height": 1200, "cores": 16, "ram": 32, "is_mobile": False,
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
}
_PRFL_DEFAULT_PRESET = _PRFL_PRESETS[1]


def _load_profile_fingerprint(profile_id: str, drive_root: str) -> dict:
    """Load or create a persistent fingerprint for a profile.

    Reads Drive/profiles/PRFL-NNN.fingerprint.json if it exists.
    Otherwise generates from _PRFL_PRESETS and saves to Drive.

    Returns a dict compatible with the existing launch_browser() fingerprint
    format: canvas_seed, audio_seed, ua_template, width, height, cores, ram,
    platform, ch_platform, ch_arch, is_mobile, webgl_renderer.

    The proxy/timezone/locale/lat/lon are NOT stored here — they are runtime-
    dynamic and resolved live by _resolve_ip_geo() as always.
    """
    if profile_id in _PROFILE_FINGERPRINT_CACHE:
        return _PROFILE_FINGERPRINT_CACHE[profile_id]

    fp_path = os.path.join(drive_root, "profiles", f"{profile_id}.fingerprint.json")

    # Try loading from Drive
    if os.path.isfile(fp_path):
        try:
            with open(fp_path) as _f:
                fp = json.load(_f)
            _PROFILE_FINGERPRINT_CACHE[profile_id] = fp
            logger.info(f"profile_fingerprint: loaded {profile_id} from {fp_path}")
            return fp
        except Exception as _e:
            logger.warning(f"profile_fingerprint: load failed for {profile_id}: {_e}")

    # Generate from preset
    try:
        _num = int(profile_id.split("-")[-1])
    except (ValueError, IndexError):
        _num = 1
    fp = dict(_PRFL_PRESETS.get(_num, _PRFL_DEFAULT_PRESET))
    fp["profile_id"] = profile_id

    # Persist to Drive (static fields only)
    try:
        os.makedirs(os.path.dirname(fp_path), exist_ok=True)
        with open(fp_path + ".tmp", "w") as _f:
            json.dump(fp, _f, indent=2)
        os.replace(fp_path + ".tmp", fp_path)
        logger.info(f"profile_fingerprint: created + saved {profile_id} → {fp_path}")
    except Exception as _e:
        logger.warning(f"profile_fingerprint: save failed for {profile_id}: {_e}")

    _PROFILE_FINGERPRINT_CACHE[profile_id] = fp
    return fp




def _is_agy_installed() -> bool:
    """Check if agy binary exists on this worker."""
    return os.path.isfile(_AGY_BIN) and os.access(_AGY_BIN, os.X_OK)


def _is_agy_authenticated() -> bool:
    """Check if agy has valid credentials on this worker."""
    if not _is_agy_installed():
        return False
    # Check composite_token_storage dir (agy >= v1.1.19)
    if os.path.isdir(_AGY_STATE_DIR):
        import re as _re
        for f in os.listdir(_AGY_STATE_DIR):
            if _re.search(r'token|credential|oauth|auth', f, _re.I) and not f.endswith(('.log', '.pbtxt')):
                try:
                    raw = open(os.path.join(_AGY_STATE_DIR, f), 'r', errors='ignore').read(4096)
                    if 'refresh_token' in raw or 'access_token' in raw:
                        return True
                except Exception:
                    pass
    # Legacy token path (agy < v1.1.19)
    if os.path.isfile(_AGY_TOKEN_FILE):
        try:
            import json as _json
            t = _json.loads(open(_AGY_TOKEN_FILE).read())
            return bool(t.get("token", {}).get("refresh_token") or t.get("refresh_token"))
        except Exception:
            pass
    return False


async def _run_agy_local(prompt: str, *, system: str = "", output_format: str = "text",
                         json_schema: str | None = None, temperature: float = 0.2,
                         max_tokens: int = 8192, timeout: int = 120,
                         model: str | None = None) -> dict:
    """Run agy CLI as a subprocess and return the result."""
    import subprocess as _sp
    import shlex

    cmd = [_AGY_BIN, f"--print={prompt}", "--dangerously-skip-permissions", "--effort=high"]
    if output_format == "json":
        cmd.append("--output-format=json")
    if json_schema:
        cmd.append(f"--json-schema={json_schema}")
    mdl = model or os.environ.get("XIOAI_AGY_MODEL")
    if mdl:
        cmd.append(f"--model={mdl}")
    if timeout > 0:
        cmd.append(f"--print-timeout={timeout}s")

    # Environment: set proxy for residential exit + unset DISPLAY to prevent Chrome conflict
    env = dict(os.environ)
    env["DISPLAY"] = ""  # Prevent agy from detecting Chrome CDP
    proxy = os.environ.get("XIOAI_AGY_PROXY") or os.environ.get("SSH_PROXY_URL", "socks5://127.0.0.1:19055")
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
    env.setdefault("HOME", os.path.expanduser("~"))
    env["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"

    logger.info(f"ai_generate: running agy local cmd_len={len(cmd)} timeout={timeout}")
    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _sp.run(
                cmd, env=env, capture_output=True, text=True,
                timeout=timeout + 10,
            ),
        )
        if proc.returncode == 0:
            return {
                "ok": True, "text": proc.stdout.strip(),
                "provider": "agy_local", "model": mdl or "default",
            }
        else:
            return {
                "ok": False, "error": f"agy exit {proc.returncode}: {proc.stderr[:500]}",
                "provider": "agy_local",
            }
    except _sp.TimeoutExpired:
        return {"ok": False, "error": f"agy timed out after {timeout}s", "provider": "agy_local"}
    except Exception as e:
        return {"ok": False, "error": str(e), "provider": "agy_local"}


class AIGenerateRequest(BaseModel):
    prompt: str
    system: str = ""
    output_format: str = "text"
    json_schema: str | None = None
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout: int = 120
    model: str | None = None
    provider: str | None = None  # "local", "remote", or None (auto)


@app.get("/ai/status")
async def ai_status() -> dict:
    """Check AI capabilities on this worker."""
    installed = _is_agy_installed()
    authenticated = _is_agy_authenticated() if installed else False
    version = None
    if installed:
        try:
            import subprocess as _sp
            version = _sp.check_output(
                [_AGY_BIN, "--version"], text=True, timeout=5,
                env={**os.environ, "DISPLAY": ""},
            ).strip()
        except Exception:
            pass

    # Check XIOSYNC server reachability for remote path
    xiosync_url = (os.environ.get("XIOSYNC_URL") or
                   os.environ.get("XIORUN_XIOSYNC_BASE") or
                   os.environ.get("XIOSYNC_BASE") or "")
    remote_available = bool(xiosync_url)

    return {
        "agy_installed": installed,
        "agy_authenticated": authenticated,
        "agy_version": version,
        "agy_binary": _AGY_BIN if installed else None,
        "remote_available": remote_available,
        "xiosync_url": xiosync_url or None,
        "preferred_provider": (
            "agy_local" if authenticated else
            "agy_remote" if remote_available else
            "none"
        ),
    }


@app.post("/ai/generate")
async def ai_generate(req: AIGenerateRequest) -> dict:
    """Generate AI content — runs local agy or proxies to XIOSYNC server.

    Provider selection:
      - "local": Force local agy (must be installed + authenticated)
      - "remote": Force proxy to XIOSYNC server
      - None/auto: local if authenticated, otherwise remote
    """
    provider = req.provider

    # Auto-detect provider
    if not provider:
        if _is_agy_authenticated():
            provider = "local"
        else:
            provider = "remote"

    if provider == "local":
        if not _is_agy_installed():
            raise HTTPException(status_code=503, detail="agy not installed on this worker. Call POST /ai/install-agy first.")
        if not _is_agy_authenticated():
            raise HTTPException(status_code=503, detail="agy not authenticated. Run agy auth flow first.")
        result = await _run_agy_local(
            req.prompt, system=req.system, output_format=req.output_format,
            json_schema=req.json_schema, temperature=req.temperature,
            max_tokens=req.max_tokens, timeout=req.timeout, model=req.model,
        )
        return result

    elif provider == "remote":
        # Proxy to XIOSYNC server's /api/v1/xioai/generate endpoint
        xiosync_url = (os.environ.get("XIOSYNC_URL") or
                       os.environ.get("XIORUN_XIOSYNC_BASE") or
                       os.environ.get("XIOSYNC_BASE") or "")
        if not xiosync_url:
            raise HTTPException(status_code=503, detail="XIOSYNC_URL not configured — cannot proxy to remote AI.")

        import urllib.request
        import json as _json
        url = f"{xiosync_url}/api/v1/xioai/generate"
        body = _json.dumps({
            "description": req.prompt,
            "system": req.system,
            "output_format": req.output_format,
            "json_schema": req.json_schema,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "timeout": req.timeout,
            "model": req.model,
        }).encode()

        # Use internal secret for auth if available
        headers = {"Content-Type": "application/json"}
        internal_secret = os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
        if internal_secret:
            headers["X-Internal-Secret"] = internal_secret

        try:
            http_req = urllib.request.Request(url, body, headers)
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: urllib.request.urlopen(http_req, timeout=req.timeout + 10).read(),
            )
            data = _json.loads(resp)
            # Map XIOSYNC response to our format
            if "text" in data:
                return {"ok": True, "text": data["text"], "provider": f"remote:{data.get('provider', '?')}", "model": data.get("model")}
            elif "script" in data:
                return {"ok": True, "text": data["script"], "provider": f"remote:{data.get('provider', '?')}", "model": data.get("model")}
            elif "error" in data or "detail" in data:
                return {"ok": False, "error": data.get("error") or data.get("detail"), "provider": "remote"}
            else:
                return {"ok": True, "text": _json.dumps(data), "provider": "remote"}
        except Exception as e:
            return {"ok": False, "error": f"Remote AI call failed: {e}", "provider": "remote"}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}. Use 'local', 'remote', or omit for auto.")


@app.post("/ai/install-agy")
async def ai_install_agy() -> dict:
    """Install agy binary and authenticate using a persisted Google Chrome profile.

    Phase 4 — Native Colab agy path:
    1. Install agy binary (from Drive cache or official installer)
    2. Restore Chrome profile from Drive profiles/ (contains active Google session)
    3. Suspend Chrome CDP so agy doesn't enter 'local chrome mode'
    4. Run agy auth PTY flow — Chrome opens, session already active, OAuth auto-consents
    5. Persist ~/.gemini/antigravity-cli/ credentials to Drive
    """
    import subprocess as _sp, shutil as _sh, gzip as _gz, pty as _pty, \
           select as _sel, signal as _sig, fcntl as _fcntl, termios as _termios

    result: dict = {
        "binary_installed": False,
        "binary_source": None,
        "auth_attempted": False,
        "auth_success": False,
        "profile_restored": False,
        "creds_persisted": False,
        "error": None,
    }

    # ── Step 1: Install binary ──────────────────────────────────────────────────
    _drive_binary = os.path.join(DRIVE_ROOT, "cache/agy-binary.gz")

    if not _is_agy_installed():
        # Try Drive cache first (fast — no network)
        if os.path.isfile(_drive_binary):
            try:
                os.makedirs(os.path.dirname(_AGY_BIN), exist_ok=True)
                with _gz.open(_drive_binary, "rb") as f_in, open(_AGY_BIN, "wb") as f_out:
                    _sh.copyfileobj(f_in, f_out)
                os.chmod(_AGY_BIN, 0o755)
                result["binary_source"] = "drive_cache"
                logger.info(f"ai_install_agy: binary restored from Drive cache")
            except Exception as e:
                logger.warning(f"ai_install_agy: Drive restore failed: {e}")

        # Official installer fallback
        if not _is_agy_installed():
            try:
                env = {**os.environ, "HOME": os.path.expanduser("~")}
                out = _sp.check_output(
                    "curl -fsSL https://antigravity.google/cli/install.sh | bash",
                    shell=True, text=True, timeout=180, env=env, stderr=_sp.STDOUT,
                )
                result["binary_source"] = "official_installer"
                logger.info("ai_install_agy: installed from official installer")
            except Exception as e:
                result["error"] = f"Binary install failed: {e}"
                return result

    if _is_agy_installed():
        result["binary_installed"] = True
        # Cache to Drive for next boot
        if not os.path.isfile(_drive_binary):
            try:
                os.makedirs(os.path.dirname(_drive_binary), exist_ok=True)
                with open(_AGY_BIN, "rb") as f_in, _gz.open(_drive_binary, "wb") as f_out:
                    f_out.write(f_in.read())
                logger.info(f"ai_install_agy: binary cached to Drive {_drive_binary}")
            except Exception as cache_err:
                logger.warning(f"ai_install_agy: Drive cache failed: {cache_err}")
    else:
        result["error"] = f"Binary not found at {_AGY_BIN} after install attempt"
        return result

    # Already authenticated? Short-circuit.
    if _is_agy_authenticated():
        result["auth_success"] = True
        result["auth_attempted"] = False
        return result

    # ── Step 2.5: Restore agy credentials from Drive cache ──────────────────────
    # The archive stores tokens under .gemini/antigravity-cli/... (arcname prefix).
    # Extract to HOME (/root) so entries unpack as /root/.gemini/antigravity-cli/...
    # Do NOT extract to ~/.gemini or paths become ~/.gemini/.gemini/... (double-nest).
    _agy_creds_cache = os.path.join(DRIVE_ROOT, "cache/agy-credentials.tar.gz")
    if os.path.isfile(_agy_creds_cache):
        try:
            _home_dir = os.path.expanduser("~")
            os.makedirs(os.path.join(_home_dir, ".gemini"), exist_ok=True)
            # Extract to HOME — arcname=".gemini/{item}" → /root/.gemini/{item}
            _sp.run(
                ["tar", "xzf", _agy_creds_cache, "-C", _home_dir],
                capture_output=True, timeout=30,
            )
            logger.info(f"ai_install_agy: credentials restored from Drive cache → {os.path.join(_home_dir, '.gemini')}")
            if _is_agy_authenticated():
                result["auth_success"] = True
                result["auth_attempted"] = False
                result["creds_persisted"] = True  # already on Drive
                logger.info("ai_install_agy: auth confirmed from restored credentials ✅")
                return result
            else:
                logger.info("ai_install_agy: credentials restored but tokens not valid (expired) — proceeding with OAuth")
        except Exception as _creds_err:
            logger.warning(f"ai_install_agy: credentials restore failed: {_creds_err}")

    # agy's OAuth opens a browser; pre-loading a profile with Google cookies
    # means the consent screen auto-fills and redirects back immediately.
    _profiles_drive = os.path.join(DRIVE_ROOT, "profiles")
    _restore_dir = None

    if os.path.isdir(_profiles_drive):
        _profile_tars = sorted([
            p for p in os.listdir(_profiles_drive)
            if p.startswith("PRFL-") and p.endswith(".tar.gz")
        ], reverse=True)  # highest serial first (most recent login)
        if _profile_tars:
            # Prefer a profile with a valid fingerprint JSON (email present)
            _tar_path = os.path.join(_profiles_drive, _profile_tars[0])
            for _pt in _profile_tars:
                _fp_path = os.path.join(_profiles_drive, _pt.replace(".tar.gz", ".fingerprint.json"))
                if os.path.isfile(_fp_path):
                    try:
                        import json as _fp_j
                        _fp_data = _fp_j.load(open(_fp_path))
                        if "@" in _fp_data.get("email", ""):
                            _tar_path = os.path.join(_profiles_drive, _pt)
                            logger.info(f"ai_install_agy: using profile {_pt} ({_fp_data['email']}) for OAuth browser")
                            break
                    except Exception:
                        pass
            _base_restore = "/tmp/agy-auth-chrome-profile"
            try:
                import tarfile as _tf_agy
                if os.path.exists(_base_restore):
                    _sh.rmtree(_base_restore)
                os.makedirs(_base_restore, exist_ok=True)
                with _tf_agy.open(_tar_path, "r:gz") as tf:
                    # Skip Chrome runtime artifacts (singleton sockets, lock files)
                    # that tar rejects as unsafe absolute symlinks.
                    _skip = {"SingletonSocket", "SingletonLock", "SingletonCookies",
                             "DevToolsActivePort", "lockfile"}
                    for _member in tf.getmembers():
                        if any(_member.name.endswith(s) for s in _skip):
                            continue
                        try:
                            tf.extract(_member, path="/tmp", filter="tar")
                        except Exception:
                            pass  # skip unextractable members silently
                # Tarball contains a single PRFL_* dir — locate it
                _children = sorted([
                    c for c in os.listdir("/tmp")
                    if c.startswith("PRFL_") and os.path.isdir(f"/tmp/{c}")
                ])
                _restore_dir = f"/tmp/{_children[0]}" if _children else None
                result["profile_restored"] = True
                result["profile_tar"] = _profile_tars[0]
                logger.info(f"ai_install_agy: restored profile {_profile_tars[0]} → {_restore_dir}")
            except Exception as pe:
                logger.warning(f"ai_install_agy: profile restore failed: {pe}")
        else:
            logger.warning("ai_install_agy: no PRFL-*.tar.gz found in Drive profiles/")
    else:
        logger.warning(f"ai_install_agy: Drive profiles/ not at {_profiles_drive}")

    # ── Step 4: Drive agy auth via tmux + ProfileManager browser ────────────────
    # agy renders its login flow as a fullscreen TUI — OAuth URL is painted via
    # cursor-positioning ANSI.  tmux -J captures the rendered cell grid so we can
    # read the URL as plain text.  Browser driving is delegated to ProfileManager
    # which pre-injects the full anti-detection fingerprint for this profile.
    #
    # Flow:
    #   a) agy in tmux → "Select login method" menu → send Enter (Google OAuth)
    #   b) agy prints OAuth URL → ProfileManager.launch_in_thread() drives browser
    #      with profile cookies + stealth JS + residential SOCKS5 proxy
    #   c) Google auto-consents (active session) → redirect to antigravity.google
    #   d) Extract auth code → send to agy tmux input
    #   e) agy writes tokens → "signed in" marker detected
    result["auth_attempted"] = True
    _auth_timeout = 240  # 4 min total

    import re as _re_auth, time as _time

    # Determine profile_id from the profile_tar we restored
    _profile_id = None
    if result.get("profile_tar"):
        _m_prfl = _re_auth.match(r"(PRFL-\d+)\.tar\.gz", result["profile_tar"])
        if _m_prfl:
            _profile_id = _m_prfl.group(1)
    if not _profile_id:
        _profile_id = "PRFL-001"

    # Load persistent fingerprint for this profile (or generate + save to Drive).
    # Returns the same dict format the existing launch_browser() machinery uses.
    _fp = _load_profile_fingerprint(_profile_id, DRIVE_ROOT)
    logger.info(f"agy_auth: fingerprint for {_profile_id}: ua={_fp.get('ua_template','')[:40]}...")

    # Proxy is passed as --proxy-server= Chrome flag inside _run_oauth_browser()
    # (matching how the existing launch_browser() does it).
    # The WS SOCKS5 bridge on :19055 must be running (started in lifespan).
    _proxy_cfg = None   # not used here — handled inside the thread via Chrome flag

    # Geo: resolve from the worker's actual public IP for TZ/locale consistency
    _exit_ip = _get_runtime_public_ip()
    _geo = _resolve_ip_geo(_exit_ip)

    import datetime as _dt
    try:
        import zoneinfo as _zi
        _tz_obj    = _zi.ZoneInfo(_geo["timezone"])
        _tz_offset = -int(_dt.datetime.now(_tz_obj).utcoffset().total_seconds() // 60)
    except Exception:
        _tz_offset = 0
    _screen_h  = str(_fp.get("height", 1080))
    _session_stealth_js = (
        _STEALTH_JS                                              # existing, proven, 22-section
        .replace("'{TIMEZONE}'",       f"'{_geo['timezone']}'")
        .replace("{TIMEZONE}",          _geo["timezone"])
        .replace("'{LOCALE}'",         f"'{_geo['locale']}'")
        .replace("{LOCALE}",            _geo["locale"])
        .replace("{CANVAS_SEED}",       str(_fp.get("canvas_seed", 0x1A2B3C)))
        .replace("{AUDIO_SEED}",        str(_fp.get("audio_seed",  0x4D5E6F)))
        .replace("{LAT}",               str(_geo["lat"]))
        .replace("{LON}",               str(_geo["lon"]))
        .replace("'{UA}'",             f"'{_fp.get('ua_template', _STEALTH_UA_CHROME)}'")
        .replace("{UA}",                _fp.get("ua_template", _STEALTH_UA_CHROME))
        .replace("{TZ_OFFSET}",         str(_tz_offset))
        .replace("{CORES}",         str(_fp.get("cores", 8)))
        .replace("{RAM}",  str(_fp.get("ram", 8)))
        .replace("{PLATFORM}",          _fp.get("platform", "Win32"))
        .replace("{CH_PLATFORM}",       _fp.get("ch_platform", "Windows"))
        .replace("{CH_ARCH}",           _fp.get("ch_arch", "x86"))
        .replace("{IS_MOBILE}",         "true" if _fp.get("is_mobile") else "false")
        .replace("{SCREEN_W}",          str(_fp.get("width", 1920)))
        .replace("{SCREEN_H}",          _screen_h)
        .replace("{SCREEN_AH}",         str(max(100, int(_screen_h) - 40)))
        .replace("{WEBGL_V}",           "Google Inc. (NVIDIA)")
        .replace("{WEBGL_R}",           _fp.get("webgl_renderer",
                 "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)"))
    )

    _tmux_session = "agy_auth_flow"
    _oauth_pattern = _re_auth.compile(
        r"https://accounts\.google\.com/o/oauth2[^\s\x00-\x1f]+"
        r"|https://[^\s\x00-\x1f]+[?&]client_id=[^\s\x00-\x1f]+"
    )

    try:
        # Kill stale tmux session
        _sp.run(["tmux", "kill-session", "-t", _tmux_session], capture_output=True)
        _time.sleep(0.3)

        # Singleton locks cleaned from restored profile before any browser launch
        if _restore_dir and os.path.isdir(_restore_dir):
            for _sl in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
                try: os.unlink(os.path.join(_restore_dir, _sl))
                except: pass

        # Wide tmux terminal — OAuth URLs are ~700 chars; -J joins wrapped lines
        _sp.run([
            "tmux", "new-session", "-d", "-s", _tmux_session, "-x", "500", "-y", "50"
        ], check=True, capture_output=True)

        _sp.run([
            "tmux", "send-keys", "-t", _tmux_session,
            f"DISPLAY=:99 {_AGY_BIN} --dangerously-skip-permissions", "Enter"
        ], capture_output=True)

        _start = _time.time()
        _state = "waiting_menu"
        _oauth_url = None
        _answered_prompts: set = set()

        while _time.time() - _start < _auth_timeout:
            _time.sleep(2)

            _cap = _sp.run(
                ["tmux", "capture-pane", "-t", _tmux_session, "-p", "-J"],
                capture_output=True, text=True
            ).stdout
            logger.debug(f"agy tmux ({_state}): {_cap[:250]!r}")

            # ── Menu: select Google OAuth ─────────────────────────────────────
            if _state == "waiting_menu":
                if "Select login method" in _cap or "Login method" in _cap:
                    if "login_method" not in _answered_prompts:
                        _time.sleep(0.4 + _time.time() % 0.3)
                        _sp.run(["tmux", "send-keys", "-t", _tmux_session, "Enter", ""], capture_output=True)
                        _answered_prompts.add("login_method")
                        _state = "waiting_url"
                        logger.info("agy_auth: selected Google OAuth (Enter)")
                if "Terms of Service" in _cap and "tos" not in _answered_prompts:
                    _sp.run(["tmux", "send-keys", "-t", _tmux_session, "y", "Enter"], capture_output=True)
                    _answered_prompts.add("tos")

            # ── URL: capture OAuth URL → drive patchright browser ─────────────
            elif _state == "waiting_url":
                if "Terms of Service" in _cap and "tos" not in _answered_prompts:
                    _sp.run(["tmux", "send-keys", "-t", _tmux_session, "y", "Enter"], capture_output=True)
                    _answered_prompts.add("tos")
                if "trust" in _cap.lower() and "folder" in _cap.lower() and "trust" not in _answered_prompts:
                    _sp.run(["tmux", "send-keys", "-t", _tmux_session, "Enter", ""], capture_output=True)
                    _answered_prompts.add("trust")

                _m = _oauth_pattern.search(_cap)
                if _m:
                    _oauth_url = _m.group(0).strip().rstrip(".")
                    logger.info(f"agy_auth: OAuth URL ({len(_oauth_url)} chars): {_oauth_url[:80]}...")
                    _state = "driving_browser"

                    # ── Browser task: runs in a thread (sync_playwright safe outside asyncio) ──
                    # Uses EXISTING _session_stealth_js (22-section, seeded, proven),
                    # CHROMIUM_ARGS (hardened), _PATCHRIGHT_CHROME, and _load_profile_fingerprint.
                    # Proxy passed as --proxy-server= Chrome flag — matching how launch_browser()
                    # does it, NOT as patchright's proxy={} kwarg (which is a different path).
                    def _run_oauth_browser():
                        """Thread target. Returns auth code string or None."""
                        import urllib.parse as _up

                        # Prefer Chrome 131 (real browser, NOT 'Chrome for Testing').
                        # Chrome for Testing shows an "automated testing" banner which
                        # is a bot signal and causes Google to reject aicode scope.
                        _chrome_131 = "/opt/chrome131/chrome"
                        if os.path.isfile(_chrome_131):
                            _chrome = _chrome_131
                            logger.info(f"agy_auth/browser: using Chrome 131 (real browser)")
                        else:
                            _chrome = _PATCHRIGHT_CHROME
                            logger.info(f"agy_auth/browser: using patchright Chromium (Chrome 131 not found)")
                        if not _chrome:
                            logger.warning("agy_auth/browser: no Chrome binary available")
                            return None

                        # Proxy strategy: try socks5:// first (residential exit — required for
                        # anti-detection). patchright opens many internal connections through the
                        # proxy which can cause timeouts on slow WS tunnel paths. If the proxy
                        # navigation times out, fall back to direct so auth completes regardless.
                        # The profile cookies (restored from Drive) handle the consent flow —
                        # the exit IP is less critical for the one-time OAuth step than for sessions.
                        _proxy_url = "socks5://127.0.0.1:19055"  # WS bridge → PPPoE residential
                        _auth_args_proxy  = list(CHROMIUM_ARGS) + [f"--proxy-server={_proxy_url}"]
                        _auth_args_direct = list(CHROMIUM_ARGS)  # no proxy — Colab direct

                        logger.info(f"agy_auth/browser: launching {_chrome} "
                                    f"profile={_restore_dir} proxy={_proxy_url} (direct fallback ready)")
                        try:
                            from patchright.sync_api import sync_playwright as _sync_pw
                            with _sync_pw() as _pw:
                                # Try proxy first
                                _ctx = _pw.chromium.launch_persistent_context(
                                    user_data_dir=_restore_dir,
                                    executable_path=_chrome,
                                    headless=True,
                                    user_agent=_fp.get("ua_template", _STEALTH_UA_CHROME),
                                    args=_auth_args_proxy,
                                    ignore_https_errors=True,
                                    timeout=30000,
                                )
                                _ctx.add_init_script(_session_stealth_js)
                                _pg = _ctx.new_page()

                                # Intercept OAuth URL: rewrite prompt=consent → prompt=select_account.
                                # With an active Google session in the profile, removing force-consent
                                # lets Google auto-redirect with a code instead of showing the
                                # consent form (which hits cloud-platform scope restriction).
                                def _rewrite_consent(route):
                                    import urllib.parse as _up2
                                    req = route.request
                                    u = req.url
                                    if "accounts.google.com/o/oauth2" in u and "prompt=consent" in u:
                                        u2 = u.replace("prompt=consent", "prompt=select_account")
                                        logger.info(f"agy_auth/browser: rewrote prompt=consent → select_account")
                                        route.continue_(url=u2)
                                    else:
                                        route.continue_()

                                _pg.route("**/o/oauth2/**", _rewrite_consent)
                                _time.sleep(0.8 + _time.time() % 0.5)
                                _nav_ok = False
                                try:
                                    _pg.goto(_oauth_url, timeout=45000, wait_until="domcontentloaded")
                                    _nav_ok = True
                                    logger.info("agy_auth/browser: proxy nav succeeded ✅")
                                except Exception as _ne:
                                    logger.warning(f"agy_auth/browser: proxy nav failed ({_ne}) — retrying direct")

                                # Fallback: close proxy ctx, relaunch direct
                                if not _nav_ok:
                                    try:
                                        _ctx.close()
                                    except Exception:
                                        pass
                                    _ctx = _pw.chromium.launch_persistent_context(
                                        user_data_dir=_restore_dir,
                                        executable_path=_chrome,
                                        headless=True,
                                        user_agent=_fp.get("ua_template", _STEALTH_UA_CHROME),
                                        args=_auth_args_direct,
                                        ignore_https_errors=True,
                                        timeout=30000,
                                    )
                                    _ctx.add_init_script(_session_stealth_js)
                                    _pg = _ctx.new_page()
                                    _pg.route("**/o/oauth2/**", _rewrite_consent)
                                    _time.sleep(0.8 + _time.time() % 0.5)
                                    logger.info("agy_auth/browser: navigating direct (no proxy)")
                                    try:
                                        _pg.goto(_oauth_url, timeout=40000, wait_until="domcontentloaded")
                                    except Exception as _ne2:
                                        logger.warning(f"agy_auth/browser: direct nav: {_ne2}")


                                _time.sleep(2)
                                logger.info(f"agy_auth/browser: at {_pg.url[:80]}")

                                # ── TOTP secret for authenticator challenges ────────────
                                import pyotp as _pyotp
                                _agy_totp_secret = os.environ.get(
                                    "XIOSYNC_TOTP_SECRET", "66pplgab2a4qzxf25tpmqs3rd4so3tuq"
                                ).replace(" ", "")

                                def _jitter_click(page, elem):
                                    """Human-like mouse move then click."""
                                    try:
                                        box = elem.bounding_box()
                                        if box:
                                            page.mouse.move(
                                                box["x"] + box["width"] / 2 + (_time.time() % 2 - 1),
                                                box["y"] + box["height"] / 2 + (_time.time() % 1.5 - 0.75),
                                            )
                                            _time.sleep(0.15 + _time.time() % 0.15)
                                        elem.click()
                                        return True
                                    except Exception:
                                        return False

                                def _first_visible(page, selectors):
                                    """Return first visible element matching any selector."""
                                    for sel in selectors:
                                        try:
                                            el = page.query_selector(sel)
                                            if el and el.is_visible():
                                                return el
                                        except Exception:
                                            pass
                                    return None

                                _last_handled = ""
                                for _wi in range(30):  # up to 90s (3s × 30)
                                    _cur = _pg.url
                                    _time.sleep(0.3)

                                    # ── 1. Callback landed ──────────────────────────────
                                    if "antigravity.google" in _cur or "oauth-callback" in _cur:
                                        logger.info(f"agy_auth/browser: ✅ [callback] {_cur[:120]}")
                                        break

                                    # ── 2. Account chooser ──────────────────────────────
                                    if ("accountchooser" in _cur or
                                            ("v3/signin" in _cur and "accountchooser" not in _last_handled)):
                                        logger.info(f"agy_auth/browser: [account_chooser] iter={_wi}")
                                        _last_handled = "accountchooser"
                                        _time.sleep(1.5)
                                        # Try clicking the target email account
                                        _clicked_acct = False
                                        try:
                                            _acct_js = """
                                            (function() {
                                                var candidates = document.querySelectorAll(
                                                    '[data-email],[data-identifier],li[data-authuser],.OVnw0d,[jsname="rymPhb"]'
                                                );
                                                for (var el of candidates) {
                                                    var em = el.getAttribute('data-email') || el.getAttribute('data-identifier') || el.textContent || '';
                                                    if (em.includes('gmail') || em.includes('@')) {
                                                        el.click(); return 'clicked:' + em.slice(0,40);
                                                    }
                                                }
                                                // Fallback: first focusable account tile
                                                var tiles = document.querySelectorAll('[tabindex="0"]');
                                                for (var t of tiles) {
                                                    if (t.textContent.includes('@gmail') || t.textContent.includes('@google')) {
                                                        t.click(); return 'clicked_tile:' + t.textContent.slice(0,30);
                                                    }
                                                }
                                                // Last resort: first li/div in the chooser
                                                var first = document.querySelector('#initialView li, .OVnw0d, [jsname="rymPhb"]');
                                                if (first) { first.click(); return 'clicked_first'; }
                                                return 'not_found';
                                            })()
                                            """
                                            _acct_res = _pg.evaluate(_acct_js)
                                            logger.info(f"agy_auth/browser: [account_chooser] JS result: {_acct_res}")
                                            _clicked_acct = True
                                        except Exception as _ae:
                                            logger.warning(f"agy_auth/browser: [account_chooser] JS error: {_ae}")
                                        if not _clicked_acct:
                                            _el = _first_visible(_pg, [
                                                "li[data-authuser]", ".OVnw0d", "[jsname='rymPhb']",
                                                "[data-email]", "[data-identifier]",
                                            ])
                                            if _el:
                                                _jitter_click(_pg, _el)
                                                logger.info("agy_auth/browser: [account_chooser] clicked element")
                                        _time.sleep(3)
                                        continue

                                    # ── 3. "Make sure it's you" / first-party confirm ───
                                    if ("nativeapp" in _cur or "firstparty" in _cur or
                                            ("oauth/v3" in _cur and "accountchooser" not in _cur)):
                                        logger.info(f"agy_auth/browser: [make_sure] iter={_wi} url={_cur[:80]}")
                                        _last_handled = "make_sure"
                                        _time.sleep(1.5)
                                        # Try confirm/continue button
                                        _confirm = _first_visible(_pg, [
                                            "button:has-text('Continue')", "button:has-text('Yes')",
                                            "button:has-text('Next')", "button:has-text('Confirm')",
                                            "button:has-text(\"Yes, it's me\")", "#next",
                                            "[jsname='LgbsSe']", "button[type='submit']",
                                        ])
                                        if _confirm:
                                            _jitter_click(_pg, _confirm)
                                            logger.info("agy_auth/browser: [make_sure] clicked confirm button")
                                            _time.sleep(3)
                                        else:
                                            logger.warning("agy_auth/browser: [make_sure] no confirm button found")
                                        continue

                                    # ── 4. Google Authenticator TOTP challenge ──────────
                                    if ("/challenge/totp" in _cur or "/challenge/ipp" in _cur):
                                        logger.info(f"agy_auth/browser: [totp] iter={_wi}")
                                        _last_handled = "totp"
                                        _time.sleep(1.0)
                                        # Wait for fresh TOTP window (>5s remaining)
                                        _totp_remaining = 30 - (_time.time() % 30)
                                        if _totp_remaining < 5:
                                            logger.info(f"agy_auth/browser: [totp] waiting {_totp_remaining:.1f}s for fresh window")
                                            _time.sleep(_totp_remaining + 0.5)
                                        _totp_code = _pyotp.TOTP(_agy_totp_secret).now()
                                        logger.info(f"agy_auth/browser: [totp] code={_totp_code}")
                                        # Find the input field
                                        _totp_input = _first_visible(_pg, [
                                            "input[type='tel']", "input[type='number']",
                                            "input[aria-label*='code' i]", "input[aria-label*='Code' i]",
                                            "input[autocomplete='one-time-code']",
                                            "input[id*='totp']", "input[id*='code']",
                                            "input[name='totpPin']", "input[name='code']",
                                            "#totpPin", "#code",
                                        ])
                                        if _totp_input:
                                            _totp_input.click()
                                            _time.sleep(0.3)
                                            _totp_input.fill("")
                                            _time.sleep(0.2)
                                            _totp_input.type(_totp_code, delay=80)
                                            _time.sleep(0.5)
                                            _next = _first_visible(_pg, [
                                                "#next", "button[type='submit']",
                                                "button:has-text('Next')", "button:has-text('Verify')",
                                                "[jsname='LgbsSe']",
                                            ])
                                            if _next:
                                                _jitter_click(_pg, _next)
                                                logger.info("agy_auth/browser: [totp] submitted code")
                                            _time.sleep(4)
                                        else:
                                            logger.warning("agy_auth/browser: [totp] no input found")
                                        continue

                                    # ── 5. agy device/display_code page ────────────────
                                    if ("/device" in _cur or "device_code" in _cur):
                                        logger.info(f"agy_auth/browser: [display_code] iter={_wi}")
                                        _last_handled = "display_code"
                                        _time.sleep(1.5)
                                        # Capture display_code from the tmux agy session
                                        try:
                                            _pane_out = _sp.check_output(
                                                ["tmux", "capture-pane", "-p", "-t", _tmux_session],
                                                text=True, timeout=5,
                                            )
                                            # agy displays a short code like "ABC-123" or "ABCD1234"
                                            _dc_match = _re_auth.search(
                                                r'\b([A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8})?)\b', _pane_out
                                            )
                                            _display_code = _dc_match.group(1) if _dc_match else ""
                                        except Exception:
                                            _display_code = ""
                                        if _display_code:
                                            logger.info(f"agy_auth/browser: [display_code] code={_display_code}")
                                            _dc_input = _first_visible(_pg, [
                                                "input[id*='code']", "input[name*='code']",
                                                "input[aria-label*='code' i]", "input[type='text']",
                                            ])
                                            if _dc_input:
                                                _dc_input.fill(_display_code)
                                                _time.sleep(0.4)
                                                _ver = _first_visible(_pg, [
                                                    "button:has-text('Verify')", "button:has-text('Next')",
                                                    "button[type='submit']", "#submit",
                                                ])
                                                if _ver:
                                                    _jitter_click(_pg, _ver)
                                                    logger.info("agy_auth/browser: [display_code] submitted")
                                                _time.sleep(3)
                                        else:
                                            logger.warning("agy_auth/browser: [display_code] no code found in tmux pane")
                                        continue

                                    # ── 6. Allow / consent screen ───────────────────────
                                    if "accounts.google.com" in _cur:
                                        logger.info(f"agy_auth/browser: [consent] iter={_wi} url={_cur[:80]}")
                                        _last_handled = "consent"
                                        _time.sleep(1.2 + _time.time() % 0.8)
                                        _allow = _first_visible(_pg, [
                                            "button[jsname='LgbsSe']",
                                            "button:has-text('Allow')",
                                            "button:has-text('Continue')",
                                            "button:has-text('Yes, I\\'m in')",
                                            "#submit_approve_access",
                                            "[data-action='allow']",
                                            "div[role='button']:has-text('Allow')",
                                        ])
                                        if _allow:
                                            _jitter_click(_pg, _allow)
                                            logger.info("agy_auth/browser: [consent] clicked Allow/Continue")
                                            _time.sleep(3)
                                        else:
                                            logger.info(f"agy_auth/browser: [consent] no button yet, waiting...")
                                        continue

                                    # Unknown page — wait and retry
                                    logger.info(f"agy_auth/browser: [waiting] iter={_wi} url={_cur[:80]}")
                                    _time.sleep(3)

                                _final = _pg.url
                                _qp = dict(_up.parse_qsl(_up.urlparse(_final).query))
                                _code = _qp.get("code", "")
                                if not _code:
                                    _frag = dict(_up.parse_qsl(_up.urlparse(_final).fragment))
                                    _code = _frag.get("code", "")
                                if not _code:
                                    try:
                                        _bt = _pg.inner_text("body")
                                        _cm2 = _re_auth.search(
                                            r"code[=:\s]+([A-Za-z0-9/_\-\.]{20,})", _bt)
                                        if _cm2: _code = _cm2.group(1)
                                    except Exception: pass

                                logger.info(f"agy_auth/browser: code={'OBTAINED' if _code else 'NONE'}")
                                _ctx.close()
                                return _code
                        except Exception as _be:
                            logger.error(f"agy_auth/browser: error: {_be}")
                            return None

                    # Run in thread — keeps the asyncio event loop free
                    _auth_code = None
                    try:
                        from concurrent.futures import ThreadPoolExecutor as _TPE
                        with _TPE(max_workers=1) as _ex:
                            _auth_code = _ex.submit(_run_oauth_browser).result(timeout=180)
                    except Exception as _te:
                        logger.error(f"agy_auth: thread executor error: {_te}")

                    if _auth_code:
                        _time.sleep(0.5)
                        _sp.run(
                            ["tmux", "send-keys", "-t", _tmux_session, _auth_code, "Enter"],
                            capture_output=True
                        )
                        logger.info("agy_auth: ✅ sent auth code to agy")
                        _state = "waiting_confirm"
                    else:
                        logger.warning("agy_auth: no auth code — agy will wait at manual prompt")
                        _state = "waiting_confirm"

            # ── Confirm: agy shows signed-in screen ──────────────────────────
            elif _state in ("waiting_confirm", "driving_browser"):
                _success_markers = [
                    "signed in", "Signed in", "logged in", "Logged in",
                    "Welcome", "Authentication successful", "Successfully authenticated",
                    "@gmail.com",  # any Google account confirmation
                ]
                if any(_mk in _cap for _mk in _success_markers):
                    result["auth_success"] = True
                    logger.info("agy_auth: ✅ authentication confirmed in TUI")
                    break

            # Session exit detection
            if _sp.run(["tmux", "list-panes", "-t", _tmux_session],
                       capture_output=True).returncode != 0:
                if _is_agy_authenticated():
                    result["auth_success"] = True
                    logger.info("agy_auth: session exited + creds found → success")
                break

        _final_cap = _sp.run(
            ["tmux", "capture-pane", "-t", _tmux_session, "-p", "-J"],
            capture_output=True, text=True
        ).stdout
        logger.info(f"agy_auth final screen: {_final_cap[:300]!r}")

    except Exception as auth_exc:
        result["error"] = f"agy auth error: {auth_exc}"
        logger.error(f"ai_install_agy: auth error: {auth_exc}")
    finally:
        _sp.run(["tmux", "kill-session", "-t", _tmux_session], capture_output=True)


    # ── Step 5: Persist agy credentials to Drive ────────────────────────────────
    # On Linux, agy stores tokens in ~/.gemini/ (no Keychain) — portable files.
    _agy_creds_src = os.path.expanduser("~/.gemini")
    _agy_creds_dst = os.path.join(DRIVE_ROOT, "cache/agy-credentials.tar.gz")
    # Persist any auth-related files (exclude heavy dirs like antigravity/brain)
    _creds_dirs = ["antigravity-cli", "google_accounts.json", "state.json", "installation_id"]

    if result["auth_success"]:
        try:
            import tarfile as _tf_cred
            os.makedirs(os.path.dirname(_agy_creds_dst), exist_ok=True)
            with _tf_cred.open(_agy_creds_dst + ".tmp", "w:gz") as tf:
                for _item in _creds_dirs:
                    _full = os.path.join(_agy_creds_src, _item)
                    if os.path.exists(_full):
                        # arcname=".gemini/{item}" + extract to HOME (/root)
                        # → /root/.gemini/{item} — correct final path.
                        # Do NOT extract to ~/.gemini or it becomes ~/.gemini/.gemini/{item}
                        tf.add(_full, arcname=os.path.join(".gemini", _item))
            _sh.move(_agy_creds_dst + ".tmp", _agy_creds_dst)
            result["creds_persisted"] = True
            logger.info(f"ai_install_agy: credentials persisted to Drive {_agy_creds_dst}")
        except Exception as cp_err:
            logger.warning(f"ai_install_agy: creds persist failed: {cp_err}")

    return result



@app.post("/ai/restore-agy-creds")
async def ai_restore_agy_creds() -> dict:
    """Restore persisted agy credentials from Drive (for runtime reboot recovery).

    On a fresh Colab runtime, call this before /ai/generate to avoid re-auth.
    Restores ~/.gemini/antigravity-cli/ from Drive cache/agy-credentials.tar.gz.
    """
    import shutil as _sh, tarfile as _tf_rc

    _src = os.path.join(DRIVE_ROOT, "cache/agy-credentials.tar.gz")
    _dst = os.path.expanduser("~/.gemini")

    if not os.path.isfile(_src):
        return {"ok": False, "reason": "No persisted credentials at Drive cache/agy-credentials.tar.gz"}

    try:
        # Extract to HOME (/root), NOT to ~/.gemini.
        # tar entries are arcname=".gemini/{item}", so extracting to HOME
        # produces /root/.gemini/{item}. Extracting to ~/.gemini would produce
        # /root/.gemini/.gemini/{item} — the double-nesting bug.
        _home = os.path.expanduser("~")
        os.makedirs(os.path.join(_home, ".gemini"), exist_ok=True)
        _agy_dir = os.path.join(_home, ".gemini", "antigravity-cli")
        if os.path.isdir(_agy_dir):
            _sh.rmtree(_agy_dir)
        with _tf_rc.open(_src, "r:gz") as tf:
            tf.extractall(path=_home)
        logger.info(f"ai_restore_agy_creds: restored to {_agy_dir}")
        return {"ok": True, "restored_to": _agy_dir, "authenticated": _is_agy_authenticated()}
    except Exception as e:
        return {"ok": False, "error": str(e)}




# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")

