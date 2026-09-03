"""Usage metering API endpoints (Gap M-3).

Provides dashboarding and billing integration endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["metering"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UsageRecordResponse(_StrictModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    period_start: str
    period_end: str
    metric_type: str
    value: int
    metadata: dict[str, Any]
    created_at: str


class UsageSummaryResponse(_StrictModel):
    organization_id: uuid.UUID
    period_start: str
    period_end: str
    metrics: dict[str, int]


def _to_record_response(rec: Any) -> UsageRecordResponse:
    return UsageRecordResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        period_start=rec.period_start.isoformat(),
        period_end=rec.period_end.isoformat(),
        metric_type=rec.metric_type,
        value=rec.value,
        metadata=rec.metadata,
        created_at=rec.created_at.isoformat(),
    )


@router.get(
    "/metering",
    response_model=UsageSummaryResponse,
    summary="Current usage summary (M-3)",
)
def get_usage_summary(
    request: Request,
    since: str | None = Query(None, description="ISO datetime for period start"),
    until: str | None = Query(None, description="ISO datetime for period end"),
) -> UsageSummaryResponse:
    """Get aggregated usage summary for the current organization."""
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.metering import MeteringService
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = MeteringService(session)
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    summary = svc.get_usage_summary(context, since=since_dt, until=until_dt)
    return UsageSummaryResponse(
        organization_id=summary.organization_id,
        period_start=summary.period_start.isoformat(),
        period_end=summary.period_end.isoformat(),
        metrics=summary.metrics,
    )


@router.get(
    "/metering/history",
    response_model=list[UsageRecordResponse],
    summary="Historical usage records (M-3)",
)
def get_usage_history(
    request: Request,
    metric_type: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
) -> list[UsageRecordResponse]:
    """Get historical usage records with optional filtering."""
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.metering import MeteringService
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = MeteringService(session)
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    records = svc.get_usage(
        context, metric_type=metric_type, since=since_dt, until=until_dt,
    )
    return [_to_record_response(r) for r in records]


@router.get(
    "/metering/{metric_type}",
    response_model=list[UsageRecordResponse],
    summary="Specific metric detail (M-3)",
)
def get_metric_detail(
    metric_type: str,
    request: Request,
    since: str | None = Query(None),
    until: str | None = Query(None),
) -> list[UsageRecordResponse]:
    """Get detail records for a specific metric type."""
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.metering import MeteringService
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = MeteringService(session)
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    records = svc.get_usage(
        context, metric_type=metric_type, since=since_dt, until=until_dt,
    )
    return [_to_record_response(r) for r in records]

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["metering"],
    dependencies=[require_capability("metering.read")],
)
