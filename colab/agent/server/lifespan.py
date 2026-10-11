"""Lifespan context manager for xiorun-agent.

Handles startup (orphan cleanup, noVNC, SOCKS5 bridge/tunnel, stealth JS, DAG poll loop)
and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess as _sp
import time as _t
from contextlib import asynccontextmanager as _acm
from typing import Any

logger = logging.getLogger("xiorun_agent")

try:
    from colab.agent.display.novnc import _NOVNC_URL, start_novnc_supervisor
except ImportError:
    try:
        from agent.display.novnc import _NOVNC_URL, start_novnc_supervisor
    except ImportError:
        def start_novnc_supervisor():
            pass

try:
    from colab.agent.dag.poll_loop import _dag_poll_loop
except ImportError:
    try:
        from agent.dag.poll_loop import _dag_poll_loop
    except ImportError:
        async def _dag_poll_loop():
            pass

# SSH SOCKS5 tunnel process — set when XIORUN_PROXY_SSH_HOST is configured
_ssh_tunnel_proc: Any = None
_SSH_PROXY_URL: str | None = None  # set at startup: "socks5://127.0.0.1:1056"
DRIVE_ROOT = os.environ.get("XIO_DRIVE_ROOT", "/content/drive/MyDrive/XIOSYNC-Shared")


def get_ssh_proxy_url() -> str | None:
    """Return the active SOCKS5 proxy URL."""
    return _SSH_PROXY_URL


def _get_active_sessions() -> dict[str, Any]:
    try:
        from colab.agent.state import _sessions as _st_sess
        return _st_sess
    except ImportError:
        try:
            from agent.state import _sessions as _st_sess
            return _st_sess
        except ImportError:
            try:
                import colab.xiorun_agent as _agent  # noqa: PLC0415
                return _agent._sessions
            except (ImportError, AttributeError):
                return {}


def _get_active_uc_drivers() -> dict[str, Any]:
    try:
        from colab.agent.state import _uc_drivers as _st_drv
        return _st_drv
    except ImportError:
        try:
            from agent.state import _uc_drivers as _st_drv
            return _st_drv
        except ImportError:
            try:
                import colab.xiorun_agent as _agent  # noqa: PLC0415
                return _agent._uc_drivers
            except (ImportError, AttributeError):
                return {}



def _load_profile_fp(pid: str, root: str) -> dict:
    try:
        import colab.xiorun_agent as _agent  # noqa: PLC0415
        return _agent._load_profile_fingerprint(pid, root)
    except (ImportError, AttributeError):
        return {}


@_acm
async def _lifespan(application):
    """Kill orphan Chrome processes; start SSH SOCKS5 tunnel + noVNC if configured."""
    global _ssh_tunnel_proc, _SSH_PROXY_URL

    # ── Kill orphan Chrome/ChromeDriver from prior agent runs ─────────────────
    for _sig in ("-TERM", "-KILL"):
        try:
            _sp.run(["pkill", _sig, "-f", "chrome-linux64/chrome"], capture_output=True)
            _sp.run(["pkill", _sig, "-f", "undetected_chromedriver"], capture_output=True)
        except Exception:
            pass
    _t.sleep(1)

    # ── noVNC: auto-start x11vnc + websockify for direct HITL control ─────────
    try:
        start_novnc_supervisor()
    except Exception as _e:
        logger.warning(f"lifespan: noVNC startup failed: {_e}")

    # ── WS SOCKS5 Bridge: route proxy traffic via XIOSYNC WebSocket tunnel ───────
    # XIOSYNC exposes /api/v1/proxy/tunnel?target=HOST:PORT — we open a local
    # SOCKS5 server on 127.0.0.1:19055 that pipes each connection through that WS.
    # This works even when Tailscale ACL blocks direct node-to-node traffic because
    # the XIOSYNC public hostname (karmas-mac-mini.taildd8b9a.ts.net) is accessible
    # via Tailscale Funnel regardless of ACL peer restrictions.
    _xiosync_base = os.environ.get("XIORUN_XIOSYNC_BASE", "")
    _internal_sec = os.environ.get("XIORUN_INTERNAL_SECRET", "")
    _pppoe_proxy = os.environ.get("XIORUN_PPPOE_PROXY", "")
    _WS_SOCKS5_PORT = 19055
    _ws_bridge_task = None

    if _xiosync_base and _internal_sec:
        # Build the WS URL: wss://karmas-mac-mini.../api/v1/proxy/tunnel?target=HOST:PORT
        _ws_base = _xiosync_base.replace("https://", "wss://").replace("http://", "ws://")
        _ws_tunnel_url = f"{_ws_base}/api/v1/proxy/tunnel?target={_pppoe_proxy}"

        async def _ws_socks5_bridge():
            """Local SOCKS5 server that tunnels each connection through XIOSYNC WS."""
            import struct

            import websockets  # noqa: PLC0415

            async def _handle_socks5_client(reader, writer):
                """Minimal SOCKS5 server: accept any user/pass, forward CONNECT to WS."""
                try:
                    # --- SOCKS5 handshake ---
                    hdr = await reader.read(3)
                    if len(hdr) < 2 or hdr[0] != 5:
                        writer.close()
                        return
                    n_methods = hdr[1]
                    if n_methods > 0:
                        await reader.read(n_methods - (len(hdr) - 2))
                    writer.write(b"\x05\x00")  # no auth required
                    await writer.drain()

                    # --- SOCKS5 request ---
                    req = await reader.read(4)
                    if len(req) < 4 or req[1] != 1:  # only CONNECT supported
                        writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
                        await writer.drain()
                        writer.close()
                        return
                    atyp = req[3]
                    if atyp == 1:  # IPv4
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
                        writer.close()
                        return
                    port_b = await reader.read(2)
                    port = struct.unpack("!H", port_b)[0]

                    # --- Open WS tunnel to XIOSYNC → PPPoE SOCKS5 proxy ---
                    # The proxy_tunnel endpoint only allows whitelisted targets (PPPoE proxy).
                    # We open a tunnel to the PPPoE SOCKS5 proxy and relay the SOCKS5
                    # CONNECT request through it so the proxy dials the actual target.
                    try:
                        import socket as _sock_mod  # noqa: PLC0415

                        ws = await websockets.connect(
                            _ws_tunnel_url,  # wss://.../proxy/tunnel?target={pppoe_host}:{port}
                            additional_headers={"x-internal-secret": _internal_sec},
                            ping_interval=20,
                            ping_timeout=20,
                            open_timeout=10,
                            family=_sock_mod.AF_INET,  # force IPv4 — Funnel has no IPv6
                        )
                    except Exception as _e:
                        logger.debug(f"ws_bridge: WS connect failed {addr}:{port} — {_e}")
                        writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)
                        await writer.drain()
                        writer.close()
                        return

                    # --- Do SOCKS5 handshake with the upstream PPPoE proxy through WS ---
                    # 1. Send greeting (no auth)
                    await ws.send(bytes([0x05, 0x01, 0x00]))
                    _greet_resp = await asyncio.wait_for(ws.recv(), timeout=5)
                    if len(_greet_resp) < 2 or _greet_resp[1] != 0:
                        logger.debug(f"ws_bridge: upstream SOCKS5 auth failed: {_greet_resp!r}")
                        writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)
                        await writer.drain()
                        writer.close()
                        await ws.close()
                        return

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
                        await writer.drain()
                        writer.close()
                        await ws.close()
                        return

                    # --- Send SOCKS5 success reply to local client ---
                    writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
                    await writer.drain()

                    # --- Bidirectional relay ---
                    async def _local_to_ws():
                        try:
                            while True:
                                d = await reader.read(65536)
                                if not d:
                                    break
                                await ws.send(d)
                        except Exception:
                            pass
                        try:
                            await ws.close()
                        except Exception:
                            pass

                    async def _ws_to_local():
                        try:
                            async for msg in ws:
                                writer.write(msg if isinstance(msg, bytes) else msg.encode())
                                await writer.drain()
                        except Exception:
                            pass
                        try:
                            writer.close()
                        except Exception:
                            pass

                    await asyncio.gather(_local_to_ws(), _ws_to_local(), return_exceptions=True)
                except Exception as _e:
                    logger.debug(f"ws_bridge: client handler error: {_e}")
                    try:
                        writer.close()
                    except Exception:
                        pass

            try:
                # Force-free the port in case a previous agent is holding it
                try:
                    _bind_sock = __import__("socket").socket()
                    _bind_sock.setsockopt(
                        __import__("socket").SOL_SOCKET, __import__("socket").SO_REUSEADDR, 1
                    )
                    _bind_sock.bind(("127.0.0.1", _WS_SOCKS5_PORT))
                    _bind_sock.close()
                except OSError:
                    # Port in use — kill the holder and wait briefly
                    _sp.run(
                        f"fuser -k {_WS_SOCKS5_PORT}/tcp 2>/dev/null || "
                        f"lsof -ti:{_WS_SOCKS5_PORT} | xargs -r kill -9 2>/dev/null || true",
                        shell=True,
                        timeout=5,
                    )
                    await asyncio.sleep(1)
                srv = await asyncio.start_server(
                    _handle_socks5_client,
                    "127.0.0.1",
                    _WS_SOCKS5_PORT,
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
                        logger.warning(
                            f"ws_bridge/supervisor: bridge crashed ({_sup_err}) — restarting in {_backoff}s"
                        )
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
    _proxy_ssh_key = os.environ.get("XIORUN_PROXY_SSH_KEY", "/root/.ssh/xio_proxy_key")
    _proxy_local_port = int(os.environ.get("XIORUN_PROXY_LOCAL_PORT", "19056"))

    if not _SSH_PROXY_URL and _proxy_ssh_host and os.path.exists(_proxy_ssh_key):
        try:
            _ssh_tunnel_proc = _sp.Popen(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ServerAliveInterval=30",
                    "-o",
                    "ServerAliveCountMax=3",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-D",
                    f"127.0.0.1:{_proxy_local_port}",
                    "-N",
                    "-i",
                    _proxy_ssh_key,
                    f"{_proxy_ssh_user}@{_proxy_ssh_host}",
                ],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            _t.sleep(3)
            if _ssh_tunnel_proc.poll() is None:
                _SSH_PROXY_URL = f"socks5://127.0.0.1:{_proxy_local_port}"
                logger.info(
                    f"SSH SOCKS5 tunnel up: {_proxy_ssh_user}@{_proxy_ssh_host} → {_SSH_PROXY_URL}"
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
    _ssh_id_file = os.environ.get("XIO_NODE_SSH_ID", f"/root/.ssh/{_node_name_ps or 'xio_node_id'}")
    _pubkey_file = f"{_ssh_id_file}.pub"
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
                    _fp_st = _load_profile_fp(_pid, DRIVE_ROOT) or {}
                    if _fp_st:
                        break
                except Exception:
                    pass
            _wr = _fp_st.get("webgl_renderer", "ANGLE (Apple, Apple M1, OpenGL 4.1)")
            _wv = "Apple" if "Apple" in _wr else "Google Inc. (NVIDIA)"
            _tz_st = _fp_st.get("timezone", "America/New_York")
            # Compute TZ offset (minutes west of UTC) for the profile timezone
            try:
                import datetime as _dt_mod
                import zoneinfo as _zi

                _tz_obj = _zi.ZoneInfo(_tz_st)
                _tz_offset_min = -int(
                    _dt_mod.datetime.now(_tz_obj).utcoffset().total_seconds() // 60
                )
            except Exception:
                _tz_offset_min = 300  # default EST

            _stealth_ua = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.6778.108 Safari/537.36"
            )
            _stealth_template = ""
            try:
                import colab.xiorun_agent as _agent  # noqa: PLC0415
                _stealth_template = _agent._STEALTH_JS
            except Exception:
                pass

            if _stealth_template:
                _sj_uc = (
                    _stealth_template.replace("{WEBGL_V}", _wv)
                    .replace("{WEBGL_R}", _wr)
                    .replace("{UA}", _fp_st.get("ua_template", _stealth_ua))
                    .replace("{PLATFORM}", _fp_st.get("platform", "Win32"))
                    .replace("{TIMEZONE}", _tz_st)
                    .replace("{TZ_OFFSET}", str(_tz_offset_min))
                    .replace("{CANVAS_SEED}", str(_fp_st.get("canvas_seed", 42)))
                    .replace("{AUDIO_SEED}", str(_fp_st.get("audio_seed", 7)))
                    .replace("{SCREEN_W}", str(_fp_st.get("width", 2560)))
                    .replace("{SCREEN_H}", str(_fp_st.get("height", 1600)))
                    .replace("{CORES}", str(_fp_st.get("cores", 10)))
                    .replace("{RAM}", str(_fp_st.get("ram", 16)))
                    .replace("'{LOCALE}'", "'en-US'")
                    .replace("{LOCALE}", "en-US")
                    .replace("{LANG}", "en-US")
                    .replace("{CH_PLATFORM}", _fp_st.get("ch_platform", "macOS"))
                    .replace("{CH_ARCH}", _fp_st.get("ch_arch", "arm"))
                    .replace("{LAT}", str(_fp_st.get("lat", 40.7128)))
                    .replace("{LON}", str(_fp_st.get("lon", -74.0060)))
                    .replace("{IS_MOBILE}", "false")
                    .replace("{SCREEN_AH}", str(_fp_st.get("height", 1600)))
                )
                for _t_target in _uc_pages[:3]:  # inject into first 3 page targets
                    try:
                        _cdp = _ws_cdp.create_connection(_t_target["webSocketDebuggerUrl"], timeout=5)

                        # Step 1: Register script for all future navigations
                        _cdp.send(
                            json.dumps(
                                {
                                    "id": 99,
                                    "method": "Page.addScriptToEvaluateOnNewDocument",
                                    "params": {"source": _sj_uc},
                                }
                            )
                        )
                        _cdp.settimeout(3)
                        try:
                            _cdp.recv()
                        except Exception:
                            pass

                        # Step 2: Run stealth JS immediately on the CURRENT page in-place
                        _cdp.send(
                            json.dumps(
                                {
                                    "id": 100,
                                    "method": "Runtime.evaluate",
                                    "params": {"expression": _sj_uc, "returnByValue": False},
                                }
                            )
                        )
                        _cdp.settimeout(4)
                        try:
                            _ev_r = json.loads(_cdp.recv())
                            _ev_err = _ev_r.get("result", {}).get("exceptionDetails")
                            if _ev_err:
                                logger.debug(f"lifespan: stealth evaluate error: {str(_ev_err)[:120]}")
                        except Exception:
                            pass

                        # Step 3: Cold-navigate: about:blank → original URL
                        _orig_url = _t_target.get("url", "")
                        if _orig_url and not _orig_url.startswith("chrome"):
                            _cdp.send(
                                json.dumps(
                                    {
                                        "id": 101,
                                        "method": "Page.navigate",
                                        "params": {"url": "about:blank"},
                                    }
                                )
                            )
                            _cdp.settimeout(4)
                            try:
                                _cdp.recv()
                            except Exception:
                                pass
                            import time as _time_mod

                            _time_mod.sleep(0.5)

                            _cdp.send(
                                json.dumps(
                                    {
                                        "id": 102,
                                        "method": "Page.navigate",
                                        "params": {"url": _orig_url},
                                    }
                                )
                            )
                            _cdp.settimeout(6)
                            try:
                                _cdp.recv()
                            except Exception:
                                pass
                            logger.info(f"lifespan: cold-navigated UC Chrome → {_orig_url[:60]}")

                        _cdp.close()
                        logger.info(f"lifespan: stealth JS active in UC Chrome: {_t_target['url'][:60]}")
                    except Exception as _ce:
                        logger.debug(f"lifespan: UC Chrome CDP inject skip: {_ce}")
    except Exception as _uce:
        logger.debug(f"lifespan: UC Chrome stealth inject (non-fatal): {_uce}")

    # ── XIOFlow DAG polling loop ─────────────────────────────────────────────
    asyncio.create_task(_dag_poll_loop())
    logger.info("lifespan: DAG polling loop started")

    yield

    # ── Graceful Shutdown (Phase 4 audit — Fix 25) ────────────────────────
    logger.info("shutdown: graceful shutdown started")
    _shutdown_errors = 0

    _sessions = _get_active_sessions()
    _uc_drivers = _get_active_uc_drivers()

    # 1. Terminate all active browser sessions
    for _sid, _info in list(_sessions.items()):
        try:
            _pid = _info.get("pid")
            if _pid:
                import signal as _sig

                os.kill(_pid, _sig.SIGTERM)
            _browser = _info.get("browser")
            if _browser:
                try:
                    await _browser.close()
                except Exception:
                    pass
            _guard = _info.get("guard_task")
            if _guard and not _guard.done():
                _guard.cancel()
        except Exception as _se:
            _shutdown_errors += 1
            logger.debug(f"shutdown: session {_sid} cleanup error: {_se}")
    _sessions.clear()

    # 2. Quit UC drivers
    for _drv in list(_uc_drivers.values()):
        try:
            _drv.quit()
        except Exception:
            _shutdown_errors += 1

    # 3. Kill SSH tunnel
    if _ssh_tunnel_proc and _ssh_tunnel_proc.poll() is None:
        _ssh_tunnel_proc.terminate()

    logger.info(f"shutdown: complete — {_shutdown_errors} errors")
