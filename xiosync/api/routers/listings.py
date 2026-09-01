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


class WorkflowItem(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    state: str
    version: int | None = None
    created_at: datetime


class WorkflowRunItem(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    workflow_id: uuid.UUID
    state: str
    created_at: datetime


class TaskItem(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    run_id: uuid.UUID
    node_id: str
    capability_id: uuid.UUID
    state: str
    priority: int
    attempts: int
    created_at: datetime


class DeadLetterItem(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    task_id: uuid.UUID
    state: str
    created_at: datetime


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


@router.get("/workflows", response_model=PaginatedResponse[WorkflowItem])
def list_workflows(
    request: Request,
    state: str | None = Query(None),
    name: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> PaginatedResponse[WorkflowItem]:
    from xiosync.persistence.models.workflows import Workflow

    filters = []
    if state:
        filters.append(Workflow.state == state)
    if name:
        filters.append(Workflow.name.ilike(f"%{name}%"))

    rows, next_cursor = _paginate(request, Workflow, limit=limit, cursor=cursor, filters=filters)
    return PaginatedResponse(
        items=[
            WorkflowItem(
                id=r.id, organization_id=r.organization_id, name=r.name,
                state=r.state, version=r.version, created_at=r.created_at,
            )
            for r in rows
        ],
        cursor=next_cursor,
    )


@router.get("/workflow-runs", response_model=PaginatedResponse[WorkflowRunItem])
def list_workflow_runs(
    request: Request,
    workflow_id: uuid.UUID | None = Query(None),
    state: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> PaginatedResponse[WorkflowRunItem]:
    from xiosync.persistence.models.workflows import WorkflowRun

    filters = []
    if workflow_id:
        filters.append(WorkflowRun.workflow_id == workflow_id)
    if state:
        filters.append(WorkflowRun.state == state)

    rows, next_cursor = _paginate(request, WorkflowRun, limit=limit, cursor=cursor, filters=filters)
    return PaginatedResponse(
        items=[
            WorkflowRunItem(
                id=r.id, organization_id=r.organization_id,
                workflow_id=r.workflow_id, state=r.state, created_at=r.created_at,
            )
            for r in rows
        ],
        cursor=next_cursor,
    )


@router.get("/tasks", response_model=PaginatedResponse[TaskItem])
def list_tasks(
    request: Request,
    state: str | None = Query(None),
    capability_id: uuid.UUID | None = Query(None),
    run_id: uuid.UUID | None = Query(None),
    priority: int | None = Query(None, ge=0, le=10),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> PaginatedResponse[TaskItem]:
    from xiosync.persistence.models.workflows import Task

    filters = []
    if state:
        filters.append(Task.state == state)
    if capability_id:
        filters.append(Task.capability_id == capability_id)
    if run_id:
        filters.append(Task.run_id == run_id)
    if priority is not None:
        filters.append(Task.priority == priority)

    rows, next_cursor = _paginate(request, Task, limit=limit, cursor=cursor, filters=filters)
    return PaginatedResponse(
        items=[
            TaskItem(
                id=r.id, organization_id=r.organization_id, run_id=r.run_id,
                node_id=r.node_id, capability_id=r.capability_id,
                state=r.state, priority=r.priority, attempts=r.attempts,
                created_at=r.created_at,
            )
            for r in rows
        ],
        cursor=next_cursor,
    )


@router.get("/dlq", response_model=PaginatedResponse[DeadLetterItem])
def list_dead_letters(
    request: Request,
    state: str | None = Query(None),
    task_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> PaginatedResponse[DeadLetterItem]:
    from xiosync.persistence.models.workflows import DeadLetter

    filters = []
    if state:
        filters.append(DeadLetter.state == state)
    if task_id:
        filters.append(DeadLetter.task_id == task_id)

    rows, next_cursor = _paginate(request, DeadLetter, limit=limit, cursor=cursor, filters=filters)
    return PaginatedResponse(
        items=[
            DeadLetterItem(
                id=r.id, organization_id=r.organization_id,
                task_id=r.task_id, state=r.state, created_at=r.created_at,
            )
            for r in rows
        ],
        cursor=next_cursor,
    )


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
