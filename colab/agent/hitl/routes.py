"""Human-in-the-loop (HITL) notice tracking, pause, and resumption endpoints."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass as _dataclass, field as _field
from datetime import UTC, datetime
from enum import StrEnum as _StrEnum
import logging
from typing import Any
import uuid

from fastapi import HTTPException
from pydantic import BaseModel

logger = logging.getLogger("xiorun_agent.hitl.routes")


class HITLState(_StrEnum):
    PENDING = "PENDING"
    RESUMED = "RESUMED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class HITLResumedBy(_StrEnum):
    HUMAN = "HUMAN"
    AI_AGENT = "AI_AGENT"
    TIMEOUT = "TIMEOUT"


@_dataclass
class HITLNotice:
    organization_id: uuid.UUID
    session_id: str
    challenge_type: str
    message: str
    id: uuid.UUID = _field(default_factory=uuid.uuid4)
    identity_id: uuid.UUID | None = None
    instructions: str | None = None
    state: HITLState = HITLState.PENDING
    created_at: datetime = _field(default_factory=lambda: datetime.now(UTC))
    resumed_at: datetime | None = None
    resumed_by: HITLResumedBy | None = None


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


class HITLCreateRequest(BaseModel):
    session_id: str
    challenge_type: str
    message: str
    instructions: str | None = None
    screenshot_path: str | None = None


class HITLResumeRequest(BaseModel):
    resumed_by: str = "human"  # human, ai_agent


async def create_hitl_notice(
    req: HITLCreateRequest,
    store: _HITLStore | None = None,
) -> dict[str, Any]:
    """Create a HITL notice for operator/AI intervention."""
    target_store = store or _hitl_store
    notice = HITLNotice(
        organization_id=uuid.uuid4(),  # worker-local org placeholder
        session_id=req.session_id,
        challenge_type=req.challenge_type,
        message=req.message,
        instructions=req.instructions,
    )
    created = target_store.create(notice)
    return {"notice_id": str(created.id), "state": created.state.value}


async def resume_hitl_notice(
    notice_id: str,
    req: HITLResumeRequest,
    store: _HITLStore | None = None,
) -> dict[str, Any]:
    """Resume a pending HITL notice (human or AI agent)."""
    target_store = store or _hitl_store
    by = HITLResumedBy.AI_AGENT if req.resumed_by == "ai_agent" else HITLResumedBy.HUMAN
    notice = target_store.resume(uuid.UUID(notice_id), resumed_by=by)
    if not notice:
        raise HTTPException(status_code=404, detail="HITL notice not found or already resolved")
    return {
        "notice_id": notice_id,
        "state": notice.state.value,
        "resumed_by": notice.resumed_by.value,
    }


async def list_pending_hitl(store: _HITLStore | None = None) -> dict[str, Any]:
    """List all pending HITL notices."""
    target_store = store or _hitl_store
    notices = target_store.list_pending()
    return {
        "notices": [
            {
                "id": str(n.id),
                "session_id": n.session_id,
                "challenge_type": n.challenge_type,
                "message": n.message,
                "state": n.state.value,
                "created_at": n.created_at.isoformat(),
            }
            for n in notices
        ]
    }
