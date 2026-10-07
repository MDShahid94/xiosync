"""proxy_tunnel.py — WebSocket TCP tunnel for Colab worker proxy access.

Colab workers join the Tailscale mesh but Tailscale ACL prevents direct
node-to-node traffic (only HTTPS to the XIOSYNC public hostname is allowed).
This endpoint bridges that gap: the worker opens a WebSocket here, and XIOSYNC
(which CAN reach the exit VM) relays raw TCP bytes bidirectionally.

Endpoint
--------
GET /api/v1/proxy/tunnel?target=HOST:PORT

Authentication: X-Internal-Secret header (same secret used for internal calls).

Usage from xiorun_agent: see the socks5_ws_bridge() helper which spins up a
local SOCKS5 port forwarded through this WebSocket tunnel.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/proxy", tags=["Proxy Tunnel"])

_INTERNAL_SECRET = os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
_ALLOWED_HOSTS = {
    "100.106.81.15",  # xiogrid-exit-vm-1
}
_ALLOWED_PORT_RANGE = (10000, 10010)  # PPPoE SOCKS5 slots only


@router.websocket("/tunnel")
async def proxy_tunnel(
    ws: WebSocket,
    target: str = Query(..., description="HOST:PORT to connect to, e.g. 100.106.81.15:10001"),
) -> None:
    """WebSocket TCP tunnel.

    The client sends/receives raw bytes; XIOSYNC relays them to ``target``.
    Only whitelisted hosts/ports are reachable (PPPoE exit VM SOCKS5 slots).
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    secret = ws.headers.get("x-internal-secret", "")
    if not _INTERNAL_SECRET or secret != _INTERNAL_SECRET:
        await ws.close(code=4403, reason="forbidden")
        return

    # ── Parse + whitelist ─────────────────────────────────────────────────────
    try:
        host, port_s = target.rsplit(":", 1)
        port = int(port_s)
    except ValueError:
        await ws.close(code=4400, reason="bad target format, expected HOST:PORT")
        return

    if host not in _ALLOWED_HOSTS:
        await ws.close(code=4403, reason=f"host {host!r} not in allowlist")
        return

    if not (_ALLOWED_PORT_RANGE[0] <= port <= _ALLOWED_PORT_RANGE[1]):
        await ws.close(code=4403, reason=f"port {port} not in allowed range")
        return

    # ── Connect to upstream ───────────────────────────────────────────────────
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
    except Exception as exc:
        logger.warning(f"proxy_tunnel: upstream connect failed: {host}:{port} — {exc}")
        await ws.close(code=4502, reason=f"upstream connect failed: {exc}")
        return

    await ws.accept()
    logger.info(f"proxy_tunnel: opened {host}:{port} (client={ws.client})")

    async def ws_to_tcp() -> None:
        try:
            while True:
                data = await ws.receive_bytes()
                writer.write(data)
                await writer.drain()
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            writer.close()

    async def tcp_to_ws() -> None:
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=60)
                if not chunk:
                    break
                await ws.send_bytes(chunk)
        except (TimeoutError, WebSocketDisconnect, Exception):
            pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)
    logger.info(f"proxy_tunnel: closed {host}:{port}")
