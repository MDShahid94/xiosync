"""exit_guard.py — Exit-node proxy sentinel.

Monitors SOCKS5 proxy liveness for a running browser session.
If the exit node becomes unreachable, the session is IMMEDIATELY terminated.

Policy: NO FALLBACK to native (Google datacenter) IP. Ever.
When the proxy is lost:
  1. Emergency cookie save (non-Google cookies only, mid-auth guard active)
  2. Terminate Chromium on Colab node
  3. Mark browser_session.state = 'failed'
  4. Cancel the running DAG task (propagates as SessionLostError)

Implementation:
  TCP-level probe to socks5 host:port every PROBE_INTERVAL_SEC.
  TCP connect success = proxy process alive.
  FAILURE_THRESHOLD consecutive failures → declare loss and kill.

Why TCP probe (not SOCKS5 handshake):
  SOCKS5 proxies respond differently by implementation. TCP connect to the
  port is reliable, fast (3s timeout), and doesn't depend on SOCKS5 version.
  Tailscale userspace-mode socks5 at :1055 responds to TCP connect when alive.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

PROBE_INTERVAL_SEC: float = 15.0   # probe every 15 seconds
FAILURE_THRESHOLD:  int   = 2      # 2 consecutive failures = 30s total grace period
TCP_PROBE_TIMEOUT:  float = 3.0    # per-probe connect timeout


def _parse_proxy(proxy_url: str) -> tuple[str, int]:
    """Parse 'socks5://host:port' → (host, port). Raises on bad format."""
    m = re.match(r"socks5://([^:]+):(\d+)", proxy_url)
    if not m:
        raise ValueError(f"Cannot parse proxy_url: {proxy_url!r}")
    return m.group(1), int(m.group(2))


async def _tcp_probe(host: str, port: int, timeout: float = TCP_PROBE_TIMEOUT) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


class ExitGuard:
    """Per-session proxy liveness monitor.

    Usage:
        guard = ExitGuard()
        task = asyncio.create_task(
            guard.monitor(session_id, proxy_url, on_loss=launcher._on_proxy_lost)
        )
        # Cancel task on graceful teardown:
        task.cancel()
    """

    def __init__(
        self,
        probe_interval: float = PROBE_INTERVAL_SEC,
        failure_threshold: int = FAILURE_THRESHOLD,
    ) -> None:
        self._probe_interval   = probe_interval
        self._failure_threshold = failure_threshold

    async def monitor(
        self,
        session_id: str,
        proxy_url: str,
        on_loss: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Run indefinitely until cancelled (graceful teardown) or proxy lost.

        Args:
            session_id: For logging only.
            proxy_url:  socks5://host:port to probe.
            on_loss:    Async callback to invoke on confirmed proxy loss.
                        Called exactly once. Must not raise.
        """
        try:
            host, port = _parse_proxy(proxy_url)
        except ValueError as exc:
            logger.error("xiorun.exit_guard.bad_proxy_url", extra={
                "session_id": session_id, "error": str(exc)
            })
            return

        failures = 0
        logger.info("xiorun.exit_guard.started", extra={
            "session_id": session_id, "proxy": proxy_url,
        })

        while True:
            await asyncio.sleep(self._probe_interval)

            reachable = await _tcp_probe(host, port)
            if reachable:
                if failures > 0:
                    logger.info("xiorun.exit_guard.proxy_recovered", extra={
                        "session_id": session_id, "failures_cleared": failures,
                    })
                failures = 0
            else:
                failures += 1
                logger.warning("xiorun.exit_guard.probe_failed", extra={
                    "session_id": session_id,
                    "consecutive_failures": failures,
                    "threshold": self._failure_threshold,
                })

                if failures >= self._failure_threshold:
                    logger.error("xiorun.exit_guard.exit_node_lost", extra={
                        "session_id": session_id,
                        "proxy_url":  proxy_url,
                        "message":    "Proxy unreachable — terminating session immediately. No IP fallback.",
                    })
                    try:
                        await on_loss()
                    except Exception as exc:
                        logger.error("xiorun.exit_guard.on_loss_error", extra={
                            "session_id": session_id, "error": str(exc)
                        })
                    return   # monitor exits — session is dead
