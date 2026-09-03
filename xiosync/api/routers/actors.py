"""Actor management API endpoints (Genesis Phase 0c — Gap G-2).

CRUD for actors — the missing REST surface that makes actor registration
a governed operation within XIOSYNC.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["actors"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateActorRequest(_StrictModel):
    actor_type: str = Field(description="Actor type (human, ai_agent, system, service, worker)")
    actor_subtype: str | None = Field(default=None, description="Actor subtype")
    role: str | None = Field(default=None, description="Scoped role reference")
    alias: str | None = Field(default=None, description="Human-readable alias/display name")
    parent_id: uuid.UUID | None = Field(default=None, description="Parent actor ID")
    state: str = Field(default="active", description="Initial state")
    lifecycle_phase: str = Field(default="operational", description="Initial lifecycle phase")
    trust_tier: str = Field(default="newcomer", description="Initial trust tier")
    health_status: str = Field(default="healthy", description="Initial health status")
    config: dict[str, Any] | None = Field(default=None, description="Immutable config")


class ActorResponse(_StrictModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    actor_type: str
    actor_subtype: str | None = None
    role: str | None = None
    alias: str | None = None
    parent_id: uuid.UUID | None = None
    state: str
    lifecycle_phase: str
    trust_tier: str
    health_status: str
    config: dict[str, Any] | None = None
    runtime_state: dict[str, Any] | None = None
    created_by: uuid.UUID | None = None
    created_at: str


class TransitionRequest(_StrictModel):
    to_state: str = Field(description="Target state for the transition")
    trigger: str = Field(default="user_command", description="What triggered this transition")
    rationale: str | None = Field(default=None, description="Reason for the transition")


def _to_response(rec: Any) -> ActorResponse:
    return ActorResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        actor_type=rec.actor_type,
        actor_subtype=rec.actor_subtype,
        role=rec.role,
        alias=rec.alias,
        parent_id=rec.parent_id,
        state=rec.state,
        lifecycle_phase=rec.lifecycle_phase,
        trust_tier=rec.trust_tier,
        health_status=rec.health_status,
        config=rec.config,
        runtime_state=rec.runtime_state,
        created_by=rec.created_by,
        created_at=rec.created_at.isoformat(),
    )


@router.post(
    "/actors",
    response_model=ActorResponse,
    status_code=201,
    summary="Create a new actor (Gap G-2)",
)
def create_actor(
    payload: CreateActorRequest,
    request: Request,
) -> ActorResponse:
    """Register a new actor (human, AI agent, service, worker) in the org."""
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.services.actors import ActorService

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = ActorService(session)
    rec = svc.create_actor(
        context,
        actor_type=payload.actor_type,
        actor_subtype=payload.actor_subtype,
        role=payload.role,
        alias=payload.alias,
        parent_id=payload.parent_id,
        state=payload.state,
        lifecycle_phase=payload.lifecycle_phase,
        trust_tier=payload.trust_tier,
        health_status=payload.health_status,
        config=payload.config,
    )
    return _to_response(rec)


@router.get(
    "/actors",
    response_model=list[ActorResponse],
    summary="List actors in the current organization",
)
def list_actors(
    request: Request,
    actor_type: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> list[ActorResponse]:
    """List actors with optional type and state filters."""
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.services.actors import ActorService

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = ActorService(session)
    records = svc.list_actors(context, actor_type=actor_type, state=state, limit=limit)
    return [_to_response(r) for r in records]


@router.get(
    "/actors/{actor_id}",
    response_model=ActorResponse,
    summary="Get a specific actor",
)
def get_actor(
    actor_id: uuid.UUID,
    request: Request,
) -> ActorResponse | JSONResponse:
    """Get actor details by ID."""
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.services.actors import ActorNotFoundError, ActorService

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = ActorService(session)
    try:
        rec = svc.get_actor(context, actor_id)
    except ActorNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/actor_not_found",
                "title": "Actor not found",
                "status": 404,
                "code": "actor_not_found",
                "actor_id": str(actor_id),
                "request_id": getattr(request.state, "request_id", ""),
            },
        )
    return _to_response(rec)


@router.post(
    "/actors/{actor_id}/transition",
    response_model=dict[str, Any],
    summary="Transition an actor's lifecycle state",
)
def transition_actor(
    actor_id: uuid.UUID,
    payload: TransitionRequest,
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """Trigger a lifecycle state transition for an actor."""
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.services.lifecycle import LifecycleService

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = LifecycleService(session)
    try:
        result = svc.transition_actor(
            context,
            actor_id=actor_id,
            to_state=payload.to_state,
            trigger=payload.trigger,
            rationale=payload.rationale,
        )
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/transition_error",
                "title": "Lifecycle transition failed",
                "status": 422,
                "code": "transition_error",
                "detail": str(exc),
                "actor_id": str(actor_id),
                "request_id": getattr(request.state, "request_id", ""),
            },
        )
    return {
        "actor_id": str(result.actor_id),
        "from_state": result.from_state,
        "to_state": result.to_state,
        "lifecycle_phase": result.lifecycle_phase,
        "operation_id": str(result.operation_id),
        "event_id": str(result.event_id),
    }

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["actors"],
    dependencies=[require_capability("actor.manage")],
)
