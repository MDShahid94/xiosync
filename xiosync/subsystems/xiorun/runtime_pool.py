"""runtime_pool.py — Process-wide registry of live CDP-attached Page objects.

Named runtime_pool (not pool.py or browser_pool.py) to avoid conceptual
confusion with xiogrid.services.browser_pools (the DB configuration layer).

This registry serves two consumers:
  1. run_dispatcher — gets the Page for the currently executing DAG
  2. XIOVIEW observe.py — gets the Page for live screenshot/CDP streaming,
     including when the session is idle (not mid-run)

Architecture note — process-local limitation (#10)
--------------------------------------------------
``XIORunRuntimePool`` is an in-process singleton backed by plain Python dicts.
This means:

* **Single-process only**: if XIOSYNC scales to multiple API/worker processes,
  a request routed to process B cannot find a Page launched by process A.
* **No crash durability**: pool state is lost on process restart.  The DB's
  ``browser_sessions`` table is the authoritative state — the pool is a
  fast-path cache.

Upgrade path (when multi-process is needed):
  1. Replace ``_launchers`` / ``_pages`` dicts with a Redis-backed lookup
     (e.g. ``{session_id: worker_ip:cdp_port}``).
  2. Have XIOVIEW proxy the CDP WS connection via the worker IP instead of
     attaching directly.
  3. ``get_runtime_pool()`` stays the same public API — only the backing
     store changes.

For single-process deployments (current architecture: one Colab worker per
session) this is correct and sufficient.
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from xiosync.subsystems.xiorun.launcher import BrowserLauncher

logger = logging.getLogger(__name__)


class XIORunRuntimePool:
    """Process-wide registry of live BrowserLauncher instances and their Pages."""

    def __init__(self) -> None:
        self._launchers: dict[str, "BrowserLauncher"] = {}
        self._pages:     dict[str, Any] = {}   # session_id → patchright Page

    # ── Public API ──────────────────────────────────────────────────────────

    async def get_or_launch(
        self,
        *,
        session_id: str,
        identity_id: str | None,
        org_id: str,
        proxy_url: str | None,
        worker_ts_ip: str,
        pool_id: str | None,
        dag_domain: str,
        engine: object,
    ) -> Any:
        """Return cached Page or orchestrate a new launch.

        If a Page is already registered for session_id, return it immediately
        (warm-pool reuse — browser was kept alive from a previous run).
        Otherwise, create a new BrowserLauncher and launch.
        """
        if session_id in self._pages:
            page = self._pages[session_id]
            if not page.is_closed():
                logger.debug("xiorun.pool.cache_hit", extra={"session_id": session_id})
                return page
            # Stale entry — remove and re-launch
            self._unregister(session_id)

        from xiosync.subsystems.xiorun.launcher import BrowserLauncher  # noqa: PLC0415

        launcher = BrowserLauncher(
            session_id=session_id,
            identity_id=identity_id,
            org_id=org_id,
            engine=engine,
        )
        page = await launcher.launch(
            proxy_url=proxy_url,
            worker_ts_ip=worker_ts_ip,
            pool_id=pool_id,
            dag_domain=dag_domain,
        )
        # _register is called inside launcher.launch() — no double-register
        return page

    def get_page(self, session_id: str) -> Any | None:
        """Synchronous Page lookup. Used by XIOVIEW _get_playwright_page()."""
        page = self._pages.get(session_id)
        if page is not None and page.is_closed():
            self._unregister(session_id)
            return None
        return page

    def get_launcher(self, session_id: str) -> "BrowserLauncher | None":
        return self._launchers.get(session_id)

    async def release(
        self,
        session_id: str,
        org_id: str,
        page_url: str | None = None,
    ) -> None:
        """Graceful teardown of a session. Called in run_dispatcher finally block."""
        launcher = self._launchers.get(session_id)
        if launcher is None:
            logger.debug("xiorun.pool.release_no_launcher", extra={"session_id": session_id})
            return
        await launcher.teardown(page_url=page_url)

    def list_sessions(self) -> list[dict]:
        """Return metadata for all registered sessions."""
        return [
            {"session_id": sid, "page_closed": page.is_closed()}
            for sid, page in self._pages.items()
        ]

    # ── Internal ────────────────────────────────────────────────────────────

    def _register(self, session_id: str, page: Any, launcher: "BrowserLauncher") -> None:
        self._pages[session_id]     = page
        self._launchers[session_id] = launcher
        logger.debug("xiorun.pool.registered", extra={
            "session_id": session_id, "total": len(self._pages)
        })

    def _unregister(self, session_id: str) -> None:
        self._pages.pop(session_id, None)
        self._launchers.pop(session_id, None)
        logger.debug("xiorun.pool.unregistered", extra={
            "session_id": session_id, "total": len(self._pages)
        })


# ── Module-level singleton ──────────────────────────────────────────────────

_pool: XIORunRuntimePool | None = None


def get_runtime_pool() -> XIORunRuntimePool:
    """Return the process-wide XIORunRuntimePool singleton."""
    global _pool
    if _pool is None:
        _pool = XIORunRuntimePool()
    return _pool
