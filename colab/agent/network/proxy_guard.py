"""colab.agent.network.proxy_guard — SOCKS5 proxy TCP probe and liveness monitor."""

from __future__ import annotations

import asyncio
import logging
import re

from colab.agent.config import NODE_NAME, XIOSYNC_BASE, XIOSYNC_TOKEN
from colab.agent.state import _sessions

logger = logging.getLogger("xiorun_agent.network.proxy_guard")


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
                        import httpx  # noqa: PLC0415

                        async with httpx.AsyncClient(timeout=5.0) as client:
                            await client.post(
                                f"{XIOSYNC_BASE}/api/v1/internal/xiorun/proxy-lost",
                                json={"session_id": session_id, "node": NODE_NAME},
                                headers={"Authorization": f"Bearer {XIOSYNC_TOKEN}"},
                            )
                    except Exception as exc:
                        logger.warning(f"proxy-lost notify failed: {exc}")
                return
