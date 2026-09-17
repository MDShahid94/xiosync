"""launcher.py — Browser lifecycle orchestration core.

One BrowserLauncher instance per active BrowserSession.
Owns the full lifecycle: launch → guard → teardown.

Launch sequence (11 steps):
  1.  Concurrency gate   — enforce browser_pools.max_instances
  2.  Fingerprint        — resolve FingerprintProfile from DB
  3.  Profile restore    — Drive Chrome tar.gz (PRIMARY)
  4.  Command Colab      — xiorun-agent POST /launch → CDP ws URL
  5.  CDP attach         — patchright.chromium.connect_over_cdp()
  6.  Fingerprint inject — JS init script + CDP UA override
  7.  Cookie fallback    — vault cookies if no Drive profile
  8.  Exit guard         — ExitGuard.monitor() task
  9.  Emergency save     — browser.on('disconnected')
  10. Mark active        — browser_sessions.state = 'active'
  11. Register           — run_dispatcher + XIORunRuntimePool

Teardown sequence:
  1. Cancel exit guard task
  2. Push profile to Drive via node_client (fire-and-forget)
  3. Cookie fallback save (only if no Drive profile existed)
  4. Terminate Chromium on Colab (node_client.terminate_browser)
  5. Mark session 'suspended' (reusable)
  6. Unregister from pool + XIOVIEW

On proxy loss (ExitGuard callback):
  Emergency save → hard kill → state='failed' → cancel DAG task
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Emergency mid-auth guard URL — activates full Google cookie skip on save
_EMERGENCY_GUARD_URL = "https://accounts.google.com/o/oauth2/emergency"


def _set_session_state(
    session_id: str,
    state: str,
    engine: object,
    worker_ts_ip: str | None = None,
) -> None:
    """Update browser_sessions.state via BrowserSessionService. Sync, no-raise.

    Uses the XIOGRID service layer instead of raw SQL.
    Caller owns no transaction — this commits internally.
    """
    try:
        from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415
        from xiosync.subsystems.xiogrid.services.browser_sessions import (  # noqa: PLC0415
            BrowserSessionService,
        )
        import uuid  # noqa: PLC0415

        with OrmSession(engine) as sess:
            svc = BrowserSessionService(sess)
            svc.set_state(
                uuid.UUID(session_id),
                state,
                worker_ts_ip=worker_ts_ip,
            )
            sess.commit()
    except Exception as exc:
        logger.warning("xiorun.launcher.set_state_error", extra={
            "session_id": session_id, "state": state, "error": str(exc)
        })


def _get_pool_max_instances(pool_id: str, engine: object) -> int:
    """Read browser_pools.max_instances. Default 10 on error."""
    try:
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

        with OrmSession(engine) as sess:
            val = sess.execute(
                text("SELECT max_instances FROM browser_pools WHERE id = :pid LIMIT 1"),
                {"pid": uuid.UUID(pool_id)},
            ).scalar()
            return int(val) if val is not None else 10
    except Exception:
        return 10


def _get_active_session_count(pool_id: str, engine: object) -> int:
    """Count active/initializing sessions in a pool."""
    try:
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

        with OrmSession(engine) as sess:
            val = sess.execute(
                text("""
                    SELECT COUNT(*) FROM browser_sessions
                    WHERE pool_id = :pid AND state IN ('initializing', 'active')
                """),
                {"pid": uuid.UUID(pool_id)},
            ).scalar()
            return int(val or 0)
    except Exception:
        return 0


class PoolFullError(RuntimeError):
    """Raised when browser_pools.max_instances is reached."""


class SessionLostError(RuntimeError):
    """Raised in the DAG executor task when exit-node is lost."""


class BrowserLauncher:
    """Owns the full lifecycle of one remote patchright browser session."""

    def __init__(
        self,
        session_id: str,
        identity_id: str | None,
        org_id: str,
        engine: object,
    ) -> None:
        self.session_id  = session_id
        self.identity_id = identity_id
        self.org_id      = org_id
        self._engine     = engine

        # Set during launch:
        self._browser: Any     = None
        self._context: Any     = None
        self._page:    Any     = None
        self._pw:      Any     = None
        self._node_client: Any = None
        self._guard_task: asyncio.Task | None = None
        self._run_task:   asyncio.Task | None = None
        self._profile_dir: str | None = None   # local path on Colab
        self._had_drive_profile: bool = False
        self._drive_object_key: str | None = None

    def set_run_task(self, task: asyncio.Task) -> None:
        """Called by run_dispatcher after creating the DAG task (for cancellation)."""
        self._run_task = task

    async def launch(
        self,
        *,
        proxy_url: str | None,
        worker_ts_ip: str,
        pool_id: str | None,
        dag_domain: str,
    ):
        """Orchestrate full browser launch. Returns live patchright Page."""
        from patchright.async_api import async_playwright  # noqa: PLC0415
        from xiosync.subsystems.xiorun.exit_guard import ExitGuard  # noqa: PLC0415
        from xiosync.subsystems.xiorun.fingerprint import (  # noqa: PLC0415
            build_cdp_ua_override,
            build_init_script,
            get_chrome_version,
            resolve_fingerprint_profile,
        )
        from xiosync.subsystems.xiorun.node_client import XIORunNodeClient  # noqa: PLC0415
        from xiosync.subsystems.xiorun.profile_store import (  # noqa: PLC0415
            ChromeProfileStore,
            lookup_drive_object_key,
        )
        from xiosync.subsystems.xiorun.session_state import SessionStateIO  # noqa: PLC0415
        from xiosync.worker.run_dispatcher import _register_page  # noqa: PLC0415

        # ── 1. Concurrency gate ────────────────────────────────────────────
        if pool_id:
            max_inst = _get_pool_max_instances(pool_id, self._engine)
            active   = _get_active_session_count(pool_id, self._engine)
            if active >= max_inst:
                raise PoolFullError(
                    f"Pool {pool_id}: {active}/{max_inst} instances active"
                )

        # ── 2. Fingerprint profile ─────────────────────────────────────────
        profile    = resolve_fingerprint_profile(self.session_id, self._engine)
        chrome_ver = get_chrome_version()
        profile_dict = {}
        if profile:
            profile_dict = {
                "webgl_vendor":   profile.webgl_vendor,
                "webgl_renderer": profile.webgl_renderer,
                "platform":       profile.platform,
                "ch_platform":    profile.ch_platform,
                "ch_arch":        profile.ch_arch,
                "cores":          profile.cores,
                "ram_gb":         profile.ram_gb,
                "screen_width":   profile.screen_width,
                "screen_height":  profile.screen_height,
                "dpr":            profile.dpr,
                "cam_name":       profile.cam_name,
                "is_mobile":      profile.is_mobile,
                "ua_template":    profile.ua_template,
            }

        # ── 3. Profile restore (Drive tar.gz — PRIMARY) ───────────────────
        self._node_client = XIORunNodeClient(worker_ts_ip)
        profile_store     = ChromeProfileStore(self._engine)

        collab_profile_dir: str | None = None
        if self.identity_id:
            self._drive_object_key = lookup_drive_object_key(self.identity_id, self._engine)
            if self._drive_object_key:
                collab_profile_dir = await self._node_client.pull_profile(
                    identity_id=self.identity_id,
                    drive_object_key=self._drive_object_key,
                )
                self._had_drive_profile = collab_profile_dir is not None

        self._profile_dir = collab_profile_dir
        logger.info("xiorun.launcher.profile_status", extra={
            "session_id":         self.session_id,
            "had_drive_profile":  self._had_drive_profile,
            "profile_dir":        collab_profile_dir,
        })

        # ── 4. Command Colab — launch Chromium ────────────────────────────
        resp = await self._node_client.launch_browser(
            session_id=self.session_id,
            proxy_url=proxy_url,
            profile_dir=collab_profile_dir,
            fingerprint=profile_dict,
            headless=True,
        )
        cdp_ws_url = resp["cdp_ws_url"]

        # ── 5. CDP attach from Mac Mini over Tailscale ────────────────────
        self._pw      = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(cdp_ws_url)
        self._context = self._browser.contexts[0]
        self._page    = self._context.pages[0] if self._context.pages else await self._context.new_page()

        # ── 6. Fingerprint injection ───────────────────────────────────────
        if profile:
            cdp_session = await self._context.new_cdp_session(self._page)
            try:
                await cdp_session.send(
                    "Network.setUserAgentOverride",
                    build_cdp_ua_override(profile, chrome_ver),
                )
            finally:
                await cdp_session.detach()
            await self._context.add_init_script(build_init_script(profile, chrome_ver))

        # ── 7. Cookie fallback (only if no Drive profile) ─────────────────
        if self.identity_id and not self._had_drive_profile:
            state = SessionStateIO(self._engine).load(self.identity_id, self.org_id)
            if state and state.get("cookies"):
                now_sec = time.time()
                valid = [
                    c for c in state["cookies"]
                    if (c.get("expires", -1) or -1) <= 0 or c["expires"] > now_sec
                ]
                if valid:
                    await self._context.add_cookies(valid)
                    logger.info("xiorun.launcher.cookie_fallback_loaded", extra={
                        "session_id": self.session_id, "count": len(valid)
                    })

        # ── 8. Exit guard ──────────────────────────────────────────────────
        if proxy_url:
            guard = ExitGuard()
            self._guard_task = asyncio.create_task(
                guard.monitor(
                    self.session_id,
                    proxy_url,
                    on_loss=self._on_proxy_lost,
                ),
                name=f"xiorun-guard-{self.session_id[:8]}",
            )

        # ── 9. Emergency save on browser disconnect ────────────────────────
        self._browser.on("disconnected", lambda: asyncio.create_task(
            self._emergency_save(),
            name=f"xiorun-emergency-{self.session_id[:8]}",
        ))

        # ── 10. Mark active ────────────────────────────────────────────────
        _set_session_state(
            self.session_id, "active", self._engine,
            worker_ts_ip=worker_ts_ip,
        )

        # ── 11. Register ───────────────────────────────────────────────────
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
        get_runtime_pool()._register(self.session_id, self._page, self)
        _register_page(self.session_id, self._page)

        logger.info("xiorun.launcher.launched", extra={
            "session_id":  self.session_id,
            "worker_ip":   worker_ts_ip,
            "cdp_ws_url":  cdp_ws_url,
        })
        return self._page

    async def teardown(self, page_url: str | None = None) -> None:
        """Graceful teardown: profile push → terminate → mark suspended."""
        # ── 1. Cancel exit guard ──────────────────────────────────────────
        if self._guard_task and not self._guard_task.done():
            self._guard_task.cancel()
            try:
                await self._guard_task
            except asyncio.CancelledError:
                pass

        # ── 2. Push profile to Drive (fire-and-forget via node_client) ────
        if self._node_client and self._profile_dir and self._drive_object_key:
            asyncio.create_task(
                self._node_client.push_profile(
                    identity_id=self.identity_id or "",
                    local_dir=self._profile_dir,
                    drive_object_key=self._drive_object_key,
                ),
                name=f"xiorun-profile-push-{self.session_id[:8]}",
            )

        # ── 3. Cookie fallback save (only if no Drive profile existed) ────
        if self.identity_id and not self._had_drive_profile:
            try:
                state = await self._context.storage_state()
                SessionStateIO(self._engine).save(  # noqa: PLC0415 (lazy import)
                    self.identity_id, self.org_id, state, page_url=page_url
                )
            except Exception as exc:
                logger.warning("xiorun.launcher.cookie_save_error", extra={
                    "session_id": self.session_id, "error": str(exc)
                })

        # ── 4. Terminate Chromium on Colab ────────────────────────────────
        if self._node_client:
            await self._node_client.terminate_browser(self.session_id)

        # ── 5. Close CDP connection ───────────────────────────────────────
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

        # ── 6. Mark suspended (reusable) + unregister ─────────────────────
        _set_session_state(self.session_id, "suspended", self._engine)
        self._unregister()

        logger.info("xiorun.launcher.torn_down", extra={"session_id": self.session_id})

    async def _on_proxy_lost(self) -> None:
        """ExitGuard callback — hard kill on exit-node loss. No cookie save for Google."""
        logger.error("xiorun.launcher.proxy_lost_killing_session", extra={
            "session_id": self.session_id
        })

        # Emergency save (non-Google cookies only)
        await self._emergency_save()

        # Terminate Chromium immediately
        if self._node_client:
            await self._node_client.terminate_browser(self.session_id)

        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

        _set_session_state(self.session_id, "failed", self._engine)
        self._unregister()

        # Cancel the running DAG task — propagates as SessionLostError
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()

    async def _emergency_save(self) -> None:
        """Called on browser.disconnected. Best-effort. Always mid-auth guard active."""
        if not self.identity_id:
            return
        try:
            if self._context and not self._context.browser.is_connected():
                return
            state = await self._context.storage_state()
            from xiosync.subsystems.xiorun.session_state import SessionStateIO  # noqa: PLC0415
            SessionStateIO(self._engine).save(
                self.identity_id, self.org_id, state,
                page_url=_EMERGENCY_GUARD_URL,
                emergency=True,
            )
        except Exception as exc:
            logger.warning("xiorun.launcher.emergency_save_error", extra={
                "session_id": self.session_id, "error": str(exc)
            })

    def _unregister(self) -> None:
        """Remove from both registries (idempotent)."""
        try:
            from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
            get_runtime_pool()._unregister(self.session_id)
        except Exception:
            pass
        try:
            from xiosync.worker.run_dispatcher import _unregister_page  # noqa: PLC0415
            _unregister_page(self.session_id)
        except Exception:
            pass
