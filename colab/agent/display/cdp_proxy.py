"""CDP Port Exposure and WebSocket Proxy (Tailscale-accessible).

Chrome remote-debugging binds to 127.0.0.1 only (chromedriver security default).
These endpoints expose CDP via port 9300 so XIOSYNC can connect from Mac Mini.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket
from pydantic import BaseModel

logger = logging.getLogger("xiorun_agent")

router = APIRouter()

try:
    from colab.agent.state import _cdp_expose_ports, _cdp_forwarders
except ImportError:
    try:
        from agent.state import _cdp_expose_ports, _cdp_forwarders
    except ImportError:
        _cdp_forwarders: dict[str, asyncio.Task] = {}  # session_id → running forwarder task
        _cdp_expose_ports: dict[str, int] = {}  # session_id → public port

_my_ts_ip: str = ""
_local_sessions: dict[str, dict[str, Any]] = {}


def _get_sessions_dict() -> dict[str, dict[str, Any]]:
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
                return _local_sessions


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


class CDPExposeRequest(BaseModel):
    session_id: str
    local_port: int  # Chrome CDP port on 127.0.0.1
    public_port: int = 0  # 0 = auto-pick a free port


class CDPProxyRequest(BaseModel):
    session_id: str | None = None


@router.post("/cdp-expose")
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
            srv = await asyncio.start_server(_forward_client, host="0.0.0.0", port=public_port)
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
        "cdp_ws_url": f"ws://{tailscale_ip}:{public_port}",
    }


@router.websocket("/cdp-ws-proxy/{session_id}")
async def cdp_ws_proxy(websocket: WebSocket, session_id: str) -> None:
    """Transparent WebSocket proxy: XIOSYNC ↔ local Chrome CDP.

    Allows patchright running on the Mac Mini to attach to UC Chrome on the
    Colab worker without requiring the Chrome CDP port to be publicly bound.
    patchright calls:
        pw.chromium.connect_over_cdp("http://{tailscale_ip}:9300/cdp-proxy/{session_id}")
    NOT YET: this WS endpoint proxies raw CDP frames bidirectionally.
    """
    import websockets as _wsl  # noqa: PLC0415

    _sessions = _get_sessions_dict()
    sess = _sessions.get(session_id, {})
    local_port = sess.get("port")
    if not local_port:
        await websocket.close(code=4404, reason="session_not_found")
        return

    # Get the actual page WebSocket URL from Chrome
    try:
        import json as _json
        import urllib.request as _ulr

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


@router.get("/cdp-proxy/{session_id}/json/version")
async def cdp_proxy_version(session_id: str) -> dict:
    """Return Chrome version info — makes this URL look like a real CDP HTTP endpoint."""
    _sessions = _get_sessions_dict()
    sess = _sessions.get(session_id, {})
    local_port = sess.get("port")
    if not local_port:
        raise HTTPException(404, detail="session_not_found")
    import json as _json
    import urllib.request as _ulr

    try:
        data = _json.loads(
            _ulr.urlopen(f"http://127.0.0.1:{local_port}/json/version", timeout=5).read()
        )
        # Override webSocketDebuggerUrl to point through this proxy
        tailscale_ip = _my_tailscale_ip()
        data["webSocketDebuggerUrl"] = f"ws://{tailscale_ip}:9300/cdp-ws-proxy/{session_id}"
        return data
    except Exception as exc:
        raise HTTPException(500, detail=str(exc)) from exc
