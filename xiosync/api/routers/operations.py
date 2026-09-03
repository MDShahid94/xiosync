"""Operations, events, artifacts, capabilities API (Phase 2 — Gap G-4)."""
from __future__ import annotations
import uuid
from typing import Any, cast
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["operations"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class AppendEventRequest(_S):
    event_type: str
    payload: dict[str, Any]
    actor_id: uuid.UUID | None = None
    severity: str | None = None

class CreateArtifactRequest(_S):
    provider_type: str
    uri: str
    content_type: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    metadata: dict[str, Any] | None = None

class CreateCapabilityRequest(_S):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    execution_mode: str = "sync"

@router.get("/operations", summary="List operations")
def list_operations(request: Request, actor_id: uuid.UUID | None = None, limit: int = 50) -> list[dict[str, Any]]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.persistence.models.operations import Operation
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    q = select(Operation).where(Operation.organization_id == ctx.organization_id)
    if actor_id:
        q = q.where(Operation.actor_id == actor_id)
    q = q.order_by(Operation.started_at.desc()).limit(limit)
    rows = session.execute(q).scalars().all()
    return [{"id": str(r.id), "operation": r.operation, "actor_id": str(r.actor_id),
             "outcome": r.outcome, "started_at": r.started_at.isoformat()} for r in rows]

@router.post("/events", status_code=201, summary="Append a standalone event", response_model=None)
def append_event(payload: AppendEventRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.events import EventService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = EventService(session)
    try:
        event_id = svc.append(ctx, event_type=payload.event_type, payload=payload.payload,
                              actor_id=payload.actor_id, severity=payload.severity)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/event_error", "title": "Event append failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(event_id), "event_type": payload.event_type}

@router.post("/artifacts", status_code=201, summary="Create a standalone artifact", response_model=None)
def create_artifact(payload: CreateArtifactRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.artifacts import ArtifactService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ArtifactService(session)
    try:
        rec = svc.create_artifact(ctx, provider_type=payload.provider_type, uri=payload.uri,
                                  created_by=ctx.actor_id, content_type=payload.content_type,
                                  size_bytes=payload.size_bytes, checksum=payload.checksum,
                                  metadata=payload.metadata)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/artifact_error", "title": "Artifact creation failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "provider_type": rec.provider_type, "uri": rec.uri}

@router.post("/capabilities", status_code=201, summary="Create a capability blueprint", response_model=None)
def create_capability(payload: CreateCapabilityRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.capabilities import CapabilityService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = CapabilityService(session)
    try:
        rec = svc.create_capability(ctx, name=payload.name, description=payload.description,
                                    input_schema=payload.input_schema, output_schema=payload.output_schema,
                                    execution_mode=payload.execution_mode)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/capability_error", "title": "Capability creation failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "name": rec.name, "state": rec.state}

@router.post("/capabilities/{capability_id}/deprecate", summary="Deprecate a capability", response_model=None)
def deprecate_capability(capability_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.capabilities import CapabilityService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = CapabilityService(session)
    try:
        svc.deprecate_capability(ctx, capability_id)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/capability_error", "title": "Deprecation failed",
            "status": 422, "detail": str(exc),
        })
    return {"capability_id": str(capability_id), "state": "deprecated"}

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["operations"],
    dependencies=[require_capability("event.manage")],
)
