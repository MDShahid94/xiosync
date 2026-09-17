"""Collection / list endpoints (Gap P-1).

Provides cursor-based paginated listing for all major entity types with
filtering and sorting. Every endpoint enforces org-scoped isolation via
``OrgContext`` from request state.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from xiosync.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    encode_cursor,
)

router = APIRouter(tags=["listings"])


# ---------------------------------------------------------------------------
# Response schemas — lightweight projections (not full service records)
# ---------------------------------------------------------------------------


class WorkerItem(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    worker_id: str
    enrollment_state: str
    pool_type: str
    software_version: str | None = None
    created_at: datetime


class EventItem(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    event_type: str
    severity: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    created_at: datetime


class CapabilityItem(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    execution_mode: str
    state: str
    version: int
    created_at: datetime


class ArtifactItem(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    provider_type: str
    uri: str
    content_type: str | None = None
    size_bytes: int | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _paginate(
    request: Request,
    model: Any,
    *,
    limit: int = 50,
    cursor: str | None = None,
    filters: list[Any] | None = None,
    order_desc: bool = True,
) -> tuple[list[Any], str | None]:
    """Run a paginated query against a model with cursor-based pagination.

    Returns ``(rows, next_cursor | None)``.
    """
    from sqlalchemy import select

    params = PaginationParams.from_query(limit=limit, cursor=cursor)
    ctx = request.state.org_context

    stmt = select(model).where(model.organization_id == ctx.organization_id)

    if filters:
        for f in filters:
            stmt = stmt.where(f)

    if params.cursor_created_at is not None and params.cursor_id is not None:
        if order_desc:
            stmt = stmt.where(
                (model.created_at < params.cursor_created_at)
                | (
                    (model.created_at == params.cursor_created_at)
                    & (model.id < params.cursor_id)
                )
            )
        else:
            stmt = stmt.where(
                (model.created_at > params.cursor_created_at)
                | (
                    (model.created_at == params.cursor_created_at)
                    & (model.id > params.cursor_id)
                )
            )

    if order_desc:
        stmt = stmt.order_by(model.created_at.desc(), model.id.desc())
    else:
        stmt = stmt.order_by(model.created_at.asc(), model.id.asc())

    stmt = stmt.limit(params.limit + 1)  # fetch one extra to detect next page

    session = request.state.org_session
    rows = list(session.scalars(stmt).all())

    next_cursor: str | None = None
    if len(rows) > params.limit:
        rows = rows[: params.limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return rows, next_cursor


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/workers", response_model=PaginatedResponse[WorkerItem])
def list_workers(
    request: Request,
    enrollment_state: str | None = Query(None),
    pool_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> PaginatedResponse[WorkerItem]:
    from xiosync.persistence.models.workers import WorkerEnrollment

    filters = []
    if enrollment_state:
        filters.append(WorkerEnrollment.enrollment_state == enrollment_state)
    if pool_type:
        filters.append(WorkerEnrollment.pool_type == pool_type)

    rows, next_cursor = _paginate(request, WorkerEnrollment, limit=limit, cursor=cursor, filters=filters)
    return PaginatedResponse(
        items=[
            WorkerItem(
                id=r.id, organization_id=r.organization_id,
                worker_id=r.worker_id, enrollment_state=r.enrollment_state,
                pool_type=r.pool_type, software_version=r.software_version,
                created_at=r.created_at,
            )
            for r in rows
        ],
        cursor=next_cursor,
    )


@router.get("/events", response_model=PaginatedResponse[EventItem])
def list_events(
    request: Request,
    event_type: str | None = Query(None),
    severity: str | None = Query(None),
    entity_type: str | None = Query(None),
    entity_id: uuid.UUID | None = Query(None),
    since: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> PaginatedResponse[EventItem]:
    from xiosync.persistence.models.authorization import Event

    filters = []
    if event_type:
        filters.append(Event.event_type == event_type)
    if severity:
        filters.append(Event.severity == severity)
    if entity_type:
        filters.append(Event.entity_type == entity_type)
    if entity_id:
        filters.append(Event.entity_id == entity_id)
    if since:
        filters.append(Event.created_at >= since)

    rows, next_cursor = _paginate(request, Event, limit=limit, cursor=cursor, filters=filters)
    return PaginatedResponse(
        items=[
            EventItem(
                id=r.id, organization_id=r.organization_id,
                event_type=r.event_type, severity=r.severity,
                entity_type=r.entity_type, entity_id=r.entity_id,
                created_at=r.created_at,
            )
            for r in rows
        ],
        cursor=next_cursor,
    )


@router.get("/capabilities", response_model=PaginatedResponse[CapabilityItem])
def list_capabilities(
    request: Request,
    state: str | None = Query(None),
    name: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> PaginatedResponse[CapabilityItem]:
    from xiosync.persistence.models.authorization import Capability

    filters = []
    if state:
        filters.append(Capability.state == state)
    if name:
        filters.append(Capability.name.ilike(f"%{name}%"))

    rows, next_cursor = _paginate(request, Capability, limit=limit, cursor=cursor, filters=filters)
    return PaginatedResponse(
        items=[
            CapabilityItem(
                id=r.id, organization_id=r.organization_id,
                name=r.name, execution_mode=r.execution_mode,
                state=r.state, version=r.version, created_at=r.created_at,
            )
            for r in rows
        ],
        cursor=next_cursor,
    )


@router.get("/artifacts", response_model=PaginatedResponse[ArtifactItem])
def list_artifacts(
    request: Request,
    provider_type: str | None = Query(None),
    content_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> PaginatedResponse[ArtifactItem]:
    from xiosync.persistence.models.artifacts import Artifact

    filters = []
    if provider_type:
        filters.append(Artifact.provider_type == provider_type)
    if content_type:
        filters.append(Artifact.content_type == content_type)

    rows, next_cursor = _paginate(request, Artifact, limit=limit, cursor=cursor, filters=filters)
    return PaginatedResponse(
        items=[
            ArtifactItem(
                id=r.id, organization_id=r.organization_id,
                provider_type=r.provider_type, uri=r.uri,
                content_type=r.content_type, size_bytes=r.size_bytes,
                created_at=r.created_at,
            )
            for r in rows
        ],
        cursor=next_cursor,
    )

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["listings"],
    dependencies=[require_capability("readonly")],
)
