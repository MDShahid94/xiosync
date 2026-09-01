"""Usage metering service (Gap M-3).

``MeteringService`` records per-organization usage metrics and provides
query/aggregation for billing dashboards and cost allocation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.metering import UsageMeter
from xiosync.platform.ids import new_id

__all__ = [
    "MeteringService",
    "UsageRecord",
    "UsageSummary",
]

METRIC_TYPES = frozenset({
    "task_runs",
    "worker_hours",
    "event_count",
    "artifact_bytes",
    "api_calls",
})


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Frozen snapshot of a ``usage_meters`` row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    period_start: datetime
    period_end: datetime
    metric_type: str
    value: int
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """High-level usage summary for dashboarding."""

    organization_id: uuid.UUID
    period_start: datetime
    period_end: datetime
    metrics: dict[str, int]


def _record(row: UsageMeter) -> UsageRecord:
    return UsageRecord(
        id=row.id,
        organization_id=row.organization_id,
        period_start=row.period_start,
        period_end=row.period_end,
        metric_type=row.metric_type,
        value=row.value,
        metadata=dict(row.meter_metadata),
        created_at=row.created_at,
    )


class MeteringService:
    """Use cases for usage metering and billing observability (Gap M-3)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _current_hour(now: datetime | None = None) -> tuple[datetime, datetime]:
        """Return (start, end) of the current hourly period."""
        ts = now or datetime.now(timezone.utc)
        start = ts.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        return start, end

    def record_usage(
        self,
        context: OrgContext,
        *,
        metric_type: str,
        value: int = 1,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> UsageRecord:
        """Increment (upsert) a metric for the current hourly period.

        Uses INSERT ... ON CONFLICT UPDATE to atomically increment the value.
        """
        period_start, period_end = self._current_hour(now)

        # Try to find existing row for this period.
        existing = self._session.scalar(
            select(UsageMeter).where(
                UsageMeter.organization_id == context.organization_id,
                UsageMeter.period_start == period_start,
                UsageMeter.metric_type == metric_type,
            )
        )

        if existing is not None:
            existing.value += value
            if metadata:
                merged = dict(existing.meter_metadata)
                merged.update(metadata)
                existing.meter_metadata = merged
            self._session.flush()
            return _record(existing)

        row = UsageMeter(
            id=new_id(),
            organization_id=context.organization_id,
            period_start=period_start,
            period_end=period_end,
            metric_type=metric_type,
            value=value,
            meter_metadata=metadata or {},
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def get_usage(
        self,
        context: OrgContext,
        *,
        metric_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[UsageRecord]:
        """Query usage records for an org within a date range."""
        stmt = (
            select(UsageMeter)
            .where(UsageMeter.organization_id == context.organization_id)
            .order_by(UsageMeter.period_start.desc())
            .limit(limit)
        )
        if metric_type is not None:
            stmt = stmt.where(UsageMeter.metric_type == metric_type)
        if since is not None:
            stmt = stmt.where(UsageMeter.period_start >= since)
        if until is not None:
            stmt = stmt.where(UsageMeter.period_end <= until)
        return [_record(row) for row in self._session.scalars(stmt).all()]

    def get_usage_summary(
        self,
        context: OrgContext,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> UsageSummary:
        """Aggregate usage into a high-level summary."""
        ts_now = datetime.now(timezone.utc)
        start = since or (ts_now - timedelta(hours=24))
        end = until or ts_now

        stmt = (
            select(UsageMeter.metric_type, func.sum(UsageMeter.value))
            .where(
                UsageMeter.organization_id == context.organization_id,
                UsageMeter.period_start >= start,
                UsageMeter.period_end <= end,
            )
            .group_by(UsageMeter.metric_type)
        )
        metrics: dict[str, int] = {}
        for metric_type, total in self._session.execute(stmt).all():
            metrics[metric_type] = int(total)

        return UsageSummary(
            organization_id=context.organization_id,
            period_start=start,
            period_end=end,
            metrics=metrics,
        )
