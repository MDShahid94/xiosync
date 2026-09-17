"""Organization resource quota enforcement (Gap M-1).

Checks current resource usage against per-org quotas stored in
``organizations.resource_quotas``. Quota keys are configurable — the platform
does not impose a fixed set; unknown keys are silently ignored. Known keys:

* ``max_workers``: maximum enrolled workers per org
* ``max_queued_tasks``: maximum tasks in ``queued`` state per org
* ``max_concurrent_runs``: maximum workflow runs in ``running`` state per org
* ``max_daily_events``: maximum events created in the current UTC day

An empty ``resource_quotas`` dict means unlimited (no enforcement).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.identity import Organization
from xiosync.persistence.models.workers import WorkerEnrollment

__all__ = ["QuotaExceededError", "QuotaService"]


class QuotaExceededError(Exception):
    """Raised when an org has exceeded a resource quota."""

    def __init__(self, resource_type: str, current: int, limit: int) -> None:
        super().__init__(
            f"quota exceeded for {resource_type}: "
            f"current={current}, limit={limit}"
        )
        self.resource_type = resource_type
        self.current = current
        self.limit = limit


class QuotaService:
    """Checks resource quotas for an organization (Gap M-1)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _get_quotas(self, organization_id: uuid.UUID) -> dict[str, int]:
        """Load the org's resource_quotas dict."""
        row = self._session.scalar(
            select(Organization.resource_quotas).where(
                Organization.id == organization_id
            )
        )
        return row if row else {}

    def check_workers(self, organization_id: uuid.UUID) -> None:
        """Raise if the org has reached max_workers."""
        quotas = self._get_quotas(organization_id)
        limit = quotas.get("max_workers")
        if limit is None:
            return
        current = self._session.scalar(
            select(func.count()).where(
                WorkerEnrollment.organization_id == organization_id,
                WorkerEnrollment.enrollment_state.in_(["pending", "approved"]),
            )
        ) or 0
        if current >= limit:
            raise QuotaExceededError("workers", current, limit)

    def check_daily_events(self, organization_id: uuid.UUID) -> None:
        """Raise if the org has reached max_daily_events for today (UTC)."""
        quotas = self._get_quotas(organization_id)
        limit = quotas.get("max_daily_events")
        if limit is None:
            return
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        current = self._session.scalar(
            select(func.count()).where(
                Event.organization_id == organization_id,
                Event.created_at >= today_start,
            )
        ) or 0
        if current >= limit:
            raise QuotaExceededError("daily_events", current, limit)
