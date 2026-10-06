"""Human-in-the-Loop (HITL) notice and resume system.

Dual interface:
  - XIOVIEW WebSocket overlays for visual monitoring
  - API for programmatic/AI-agent-driven resume

A HITL notice is created when a browser automation step encounters an
unresolvable challenge (TOTP rejected, unsupported 2FA, CAPTCHA, etc.).
The workflow blocks until an operator or AI agent resumes it.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import StrEnum

logger = logging.getLogger(__name__)

DEFAULT_HITL_TIMEOUT = 300.0

class HITLState(StrEnum):
    PENDING = "PENDING"
    RESUMED = "RESUMED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

class HITLResumedBy(StrEnum):
    HUMAN = "HUMAN"
    AI_AGENT = "AI_AGENT"
    TIMEOUT = "TIMEOUT"

@dataclass
class HITLNotice:
    organization_id: uuid.UUID
    session_id: str
    challenge_type: str
    message: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    identity_id: uuid.UUID | None = None
    instructions: str | None = None
    screenshot_object_key: str | None = None
    state: HITLState = HITLState.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    resumed_at: datetime | None = None
    resumed_by: HITLResumedBy | None = None

class HITLNoticeStore:
    def __init__(self):
        self._notices: dict[uuid.UUID, HITLNotice] = {}
        self._events: dict[uuid.UUID, asyncio.Event] = {}

    def create(self, notice: HITLNotice) -> HITLNotice:
        self._notices[notice.id] = notice
        self._events[notice.id] = asyncio.Event()
        return notice

    def get(self, notice_id: uuid.UUID) -> HITLNotice | None:
        return self._notices.get(notice_id)

    def list_pending(self, org_id: uuid.UUID | None = None) -> list[HITLNotice]:
        pending = [n for n in self._notices.values() if n.state == HITLState.PENDING]
        if org_id:
            pending = [n for n in pending if n.organization_id == org_id]
        return pending

    def resume(self, notice_id: uuid.UUID, resumed_by: HITLResumedBy = HITLResumedBy.HUMAN) -> HITLNotice:
        notice = self.get(notice_id)
        if notice and notice.state == HITLState.PENDING:
            notice.state = HITLState.RESUMED
            notice.resumed_at = datetime.now(UTC)
            notice.resumed_by = resumed_by
            if notice_id in self._events:
                self._events[notice_id].set()
        return notice

    def cancel(self, notice_id: uuid.UUID) -> HITLNotice:
        notice = self.get(notice_id)
        if notice and notice.state == HITLState.PENDING:
            notice.state = HITLState.CANCELLED
            if notice_id in self._events:
                self._events[notice_id].set()
        return notice

    async def wait_for_resume(self, notice_id: uuid.UUID, timeout: float = 300.0) -> HITLNotice:
        notice = self.get(notice_id)
        if not notice:
            raise ValueError("Notice not found")
            
        if notice.state != HITLState.PENDING:
            return notice
            
        event = self._events.get(notice_id)
        if not event:
            event = asyncio.Event()
            self._events[notice_id] = event
            
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            notice.state = HITLState.EXPIRED
            notice.resumed_by = HITLResumedBy.TIMEOUT
            notice.resumed_at = datetime.now(UTC)
            raise TimeoutError("HITL notice expired")
            
        return notice

_hitl_store = HITLNoticeStore()
