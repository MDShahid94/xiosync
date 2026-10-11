"""colab.agent.network.socks5_bridge — WebSocket SOCKS5 bridge and supervisor."""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import subprocess
import time
from typing import Any

from colab.agent.config import INTERNAL_SECRET, PPPOE_PROXY, XIOSYNC_BASE, _WS_SOCKS5_PORT

logger = logging.getLogger("xiorun_agent.network.socks5_bridge")


class _WsSocksBridgeServer:
    """SOCKS5 server that tunnels connections through XIOSYNC WebSocket endpoint."""

    def __init__(
        self,
        ws_tunnel_url: str,
        internal_secret: str,
        port: int = _WS_SOCKS5_PORT,
    ) -> None:
        self.ws_tunnel_url = ws_tunnel_url
        self.internal_secret = internal_secret
        self.port = port
        self.server: asyncio.Server | None = None

    async def handle_socks5_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Minimal SOCKS5 server: accept any user/pass, forward CONNECT to WS."""
        try:
            import websockets  # noqa: PLC0415

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
                    self.ws_tunnel_url,  # wss://.../proxy/tunnel?target={pppoe_host}:{port}
                    additional_headers={"x-internal-secret": self.internal_secret},
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

    async def serve(self) -> None:
        # Force-free the port in case a previous agent is holding it
        try:
            _bind_sock = __import__("socket").socket()
            _bind_sock.setsockopt(
                __import__("socket").SOL_SOCKET, __import__("socket").SO_REUSEADDR, 1
            )
            _bind_sock.bind(("127.0.0.1", self.port))
            _bind_sock.close()
        except OSError:
            # Port in use — kill the holder and wait briefly
            subprocess.run(
                f"fuser -k {self.port}/tcp 2>/dev/null || "
                f"lsof -ti:{self.port} | xargs -r kill -9 2>/dev/null || true",
                shell=True,
                timeout=5,
            )
            await asyncio.sleep(1)

        self.server = await asyncio.start_server(
            self.handle_socks5_client,
            "127.0.0.1",
            self.port,
            reuse_address=True,
        )
        async with self.server:
            await self.server.serve_forever()


async def _run_bridge(
    ws_tunnel_url: str,
    internal_secret: str,
    port: int = _WS_SOCKS5_PORT,
) -> None:
    """Run a single instance of the WS SOCKS5 bridge server."""
    server = _WsSocksBridgeServer(ws_tunnel_url, internal_secret, port=port)
    await server.serve()


async def _ws_socks5_supervisor(
    ws_tunnel_url: str,
    internal_secret: str,
    port: int = _WS_SOCKS5_PORT,
) -> None:
    """Self-restarting supervisor loop for WS SOCKS5 bridge with exponential backoff."""
    backoff = 1.0
    while True:
        try:
            logger.info(f"ws_bridge/supervisor: starting bridge on :{port}")
            await _run_bridge(ws_tunnel_url, internal_secret, port=port)
            logger.warning("ws_bridge/supervisor: bridge exited — restarting...")
        except asyncio.CancelledError:
            logger.info("ws_bridge/supervisor: cancelled, stopping")
            break
        except Exception as _sup_err:
            logger.warning(
                f"ws_bridge/supervisor: bridge crashed ({_sup_err}) — restarting in {backoff}s"
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)  # exponential backoff, cap 30s


async def start_ws_socks5_bridge() -> tuple[asyncio.Task | None, str | None]:
    """Test and start the WS SOCKS5 bridge if configured.

    Returns:
        (supervisor_task, proxy_url) or (None, None)
    """
    _xiosync_base = XIOSYNC_BASE
    _internal_sec = INTERNAL_SECRET
    _pppoe_proxy = PPPOE_PROXY

    if not (_xiosync_base and _internal_sec):
        return None, None

    _ws_base = _xiosync_base.replace("https://", "wss://").replace("http://", "ws://")
    _ws_tunnel_url = f"{_ws_base}/api/v1/proxy/tunnel?target={_pppoe_proxy}"

    try:
        import websockets as _wsd  # noqa: PLC0415

        _test_ws = await asyncio.wait_for(
            _wsd.connect(
                _ws_tunnel_url,
                additional_headers={"x-internal-secret": _internal_sec},
                open_timeout=8,
                family=__import__("socket").AF_INET,  # force IPv4
            ),
            timeout=10,
        )
        await _test_ws.close()

        task = asyncio.create_task(
            _ws_socks5_supervisor(_ws_tunnel_url, _internal_sec, _WS_SOCKS5_PORT)
        )
        time.sleep(0.5)
        proxy_url = f"socks5://127.0.0.1:{_WS_SOCKS5_PORT}"
        logger.info(
            f"WS SOCKS5 bridge supervisor ready on :{_WS_SOCKS5_PORT} "
            f"→ XIOSYNC tunnel → {_pppoe_proxy}"
        )
        return task, proxy_url
    except Exception as _e:
        logger.warning(f"WS SOCKS5 bridge unavailable: {_e} — will try SSH tunnel")
        return None, None
