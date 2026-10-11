"""colab.agent.state — Global state registries and stores for XIORUN agent."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from colab.agent.models import HITLNotice, HITLResumedBy, HITLState

# ── Session registry ───────────────────────────────────────────────────────────
# session_id → {"browser": Browser, "pid": int, "port": int,
#               "cdp_ws_url": str, "proxy_url": str|None,
#               "profile_dir": str|None, "guard_task": Task}
_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = asyncio.Lock()  # Atomic capacity check + registration

# UC driver registry — keeps selenium UC drivers alive after login so the
# authenticated Chrome process remains open and usable without switching browsers.
_uc_drivers: dict[str, Any] = {}  # session_id → uc.Chrome driver

# Holds UC Chrome drivers that were pre-launched via /run-uc-login-start, waiting for login
_uc_pending_drivers: dict[str, Any] = {}  # session_id → driver

# ── CDP Port Exposure (Tailscale-accessible) ──────────────────────────────────
_cdp_forwarders: dict[str, asyncio.Task] = {}  # session_id → running forwarder task
_cdp_expose_ports: dict[str, int] = {}  # session_id → public port


# ── HITL Store ────────────────────────────────────────────────────────────────
class _HITLStore:
    def __init__(self) -> None:
        self._notices: dict[uuid.UUID, HITLNotice] = {}
        self._events: dict[uuid.UUID, asyncio.Event] = {}

    def create(self, notice: HITLNotice) -> HITLNotice:
        self._notices[notice.id] = notice
        self._events[notice.id] = asyncio.Event()
        return notice

    def get(self, notice_id: uuid.UUID) -> HITLNotice | None:
        return self._notices.get(notice_id)

    def list_pending(self) -> list[HITLNotice]:
        return [n for n in self._notices.values() if n.state == HITLState.PENDING]

    def resume(
        self, notice_id: uuid.UUID, resumed_by: HITLResumedBy = HITLResumedBy.HUMAN
    ) -> HITLNotice | None:
        notice = self.get(notice_id)
        if notice and notice.state == HITLState.PENDING:
            notice.state = HITLState.RESUMED
            notice.resumed_at = datetime.now(UTC)
            notice.resumed_by = resumed_by
            if notice_id in self._events:
                self._events[notice_id].set()
        return notice


_hitl_store = _HITLStore()
