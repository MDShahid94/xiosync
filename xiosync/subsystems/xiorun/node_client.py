"""node_client.py — Async HTTP client to the xiorun-agent on Colab workers.

All calls go to http://{tailscale_ip}:9300/ over Tailscale VPN.
The xiorun-agent is a thin FastAPI process started by boot.py Phase 7 on each
Colab worker. It handles launching/terminating patchright Chromium processes
and performing direct Colab ↔ R2 profile transfers (avoiding a Mac → Colab hop).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

AGENT_PORT: int = 9300
AGENT_TIMEOUT: float = 30.0      # launch can take ~10s on cold start
PROFILE_TIMEOUT: float = 120.0   # R2 profile pull/push can take longer


class XIORunNodeClient:
    """Async HTTP client for one Colab worker's xiorun-agent."""

    def __init__(self, tailscale_ip: str, port: int = AGENT_PORT) -> None:
        self._base = f"http://{tailscale_ip}:{port}"
        self._tailscale_ip = tailscale_ip

    async def health(self) -> bool:
        """GET /health — returns True if agent is reachable and healthy."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base}/health")
                return resp.status_code == 200 and resp.json().get("ok", False)
        except Exception:
            return False

    async def launch_browser(
        self,
        *,
        session_id: str,
        proxy_url: str | None,
        profile_dir: str | None,
        fingerprint: dict[str, Any],
        headless: bool = True,
    ) -> dict[str, Any]:
        """POST /launch — start Chromium for a session.

        Args:
            session_id:  UUID of the browser_session DB row.
            proxy_url:   socks5://host:port to pass as --proxy-server. None = no proxy.
            profile_dir: Local path on the Colab node of the extracted Chrome profile dir.
                         None = fresh Chromium profile (blank session).
            fingerprint: Serialised FingerprintProfile fields for CDP UA override.
            headless:    Whether to run headless (always True on Colab).

        Returns:
            {"cdp_ws_url": "ws://100.x.x.y:PORT", "pid": <int>, "port": <int>}

        Raises:
            httpx.HTTPStatusError on non-2xx response.
            RuntimeError if response is missing cdp_ws_url.
        """
        payload = {
            "session_id":  session_id,
            "proxy_url":   proxy_url,
            "profile_dir": profile_dir,
            "fingerprint": fingerprint,
            "headless":    headless,
        }
        async with httpx.AsyncClient(timeout=AGENT_TIMEOUT) as client:
            resp = await client.post(f"{self._base}/launch", json=payload)
            resp.raise_for_status()
            data = resp.json()

        if "cdp_ws_url" not in data:
            raise RuntimeError(
                f"xiorun-agent /launch missing cdp_ws_url: {data}"
            )

        logger.info("xiorun.node_client.launched", extra={
            "session_id": session_id,
            "node_ip":    self._tailscale_ip,
            "cdp_ws_url": data["cdp_ws_url"],
        })
        return data

    async def terminate_browser(self, session_id: str) -> None:
        """POST /terminate — kill the Chromium process for this session."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base}/terminate",
                    json={"session_id": session_id},
                )
                resp.raise_for_status()
        except Exception as exc:
            # Best-effort — log and continue (process may already be dead)
            logger.warning("xiorun.node_client.terminate_error", extra={
                "session_id": session_id,
                "node_ip":    self._tailscale_ip,
                "error":      str(exc),
            })

    async def pull_profile(
        self,
        identity_id: str,
        drive_object_key: str,
    ) -> str | None:
        """POST /pull-profile — agent fetches tar.gz from Drive and extracts locally.

        This runs Colab ↔ Drive directly, avoiding a Mac → Colab profile hop.

        Returns local profile dir path on the Colab node, or None if not in Drive.
        """
        try:
            async with httpx.AsyncClient(timeout=PROFILE_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._base}/pull-profile",
                    json={"identity_id": identity_id, "drive_object_key": drive_object_key},
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
                return data.get("profile_dir")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        except Exception as exc:
            logger.warning("xiorun.node_client.pull_profile_error", extra={
                "identity_id":      identity_id,
                "drive_object_key": drive_object_key,
                "error":            str(exc),
            })
            return None

    async def push_profile(
        self,
        identity_id: str,
        local_dir: str,
        drive_object_key: str,
    ) -> None:
        """POST /push-profile — agent tars local dir and uploads to Drive.

        Fire-and-forget safe (caller does not need to await result).
        """
        try:
            async with httpx.AsyncClient(timeout=PROFILE_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._base}/push-profile",
                    json={
                        "identity_id":      identity_id,
                        "local_dir":        local_dir,
                        "drive_object_key": drive_object_key,
                    },
                )
                resp.raise_for_status()
            logger.info("xiorun.node_client.profile_pushed", extra={
                "identity_id":      identity_id,
                "drive_object_key": drive_object_key,
            })
        except Exception as exc:
            logger.warning("xiorun.node_client.push_profile_error", extra={
                "identity_id":      identity_id,
                "drive_object_key": drive_object_key,
                "error":            str(exc),
            })
