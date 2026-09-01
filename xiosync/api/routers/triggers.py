"""Workflow trigger API endpoints (Gap R-5).

CRUD for cron, event, and webhook triggers.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["triggers"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTriggerRequest(_StrictModel):
    workflow_id: uuid.UUID
    trigger_type: str = Field(description="One of: cron, event, webhook")
    config: dict[str, Any] = Field(description="Trigger-specific configuration")
    created_by: uuid.UUID


class TriggerResponse(_StrictModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    workflow_id: uuid.UUID
    trigger_type: str
    config: dict[str, Any]
    state: str
    last_fired_at: str | None = None
    next_fire_at: str | None = None
    created_at: str
    created_by: uuid.UUID


def _to_response(rec: Any) -> TriggerResponse:
    return TriggerResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        workflow_id=rec.workflow_id,
        trigger_type=rec.trigger_type,
        config=rec.config,
        state=rec.state,
        last_fired_at=rec.last_fired_at.isoformat() if rec.last_fired_at else None,
        next_fire_at=rec.next_fire_at.isoformat() if rec.next_fire_at else None,
        created_at=rec.created_at.isoformat(),
        created_by=rec.created_by,
    )


@router.post(
    "/triggers",
    response_model=TriggerResponse,
    status_code=201,
    summary="Create a workflow trigger (R-5)",
)
def create_trigger(
    payload: CreateTriggerRequest,
    request: Request,
) -> TriggerResponse:
    """Create a cron, event, or webhook trigger for a workflow."""
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.triggers import TriggerService
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = TriggerService(session)
    rec = svc.create_trigger(
        context,
        workflow_id=payload.workflow_id,
        trigger_type=payload.trigger_type,
        config=payload.config,
        created_by=payload.created_by,
    )
    return _to_response(rec)


@router.get(
    "/triggers",
    response_model=list[TriggerResponse],
    summary="List workflow triggers (R-5)",
)
def list_triggers(
    request: Request,
    state: str | None = None,
    trigger_type: str | None = None,
    workflow_id: uuid.UUID | None = None,
) -> list[TriggerResponse]:
    """List triggers for the current organization."""
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.triggers import TriggerService
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = TriggerService(session)
    recs = svc.list_triggers(
        context,
        state=state,
        trigger_type=trigger_type,
        workflow_id=workflow_id,
    )
    return [_to_response(r) for r in recs]


@router.get(
    "/triggers/{trigger_id}",
    response_model=TriggerResponse,
    summary="Get trigger details (R-5)",
)
def get_trigger(
    trigger_id: uuid.UUID,
    request: Request,
) -> TriggerResponse:
    """Get details for a specific trigger."""
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.triggers import TriggerService
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = TriggerService(session)
    rec = svc.get_trigger(context, trigger_id)
    return _to_response(rec)


@router.put(
    "/triggers/{trigger_id}/pause",
    response_model=TriggerResponse,
    summary="Pause a trigger (R-5)",
)
def pause_trigger(
    trigger_id: uuid.UUID,
    request: Request,
) -> TriggerResponse:
    """Pause a trigger to stop it from firing."""
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.triggers import TriggerService
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = TriggerService(session)
    rec = svc.pause_trigger(context, trigger_id)
    return _to_response(rec)


@router.put(
    "/triggers/{trigger_id}/resume",
    response_model=TriggerResponse,
    summary="Resume a paused trigger (R-5)",
)
def resume_trigger(
    trigger_id: uuid.UUID,
    request: Request,
) -> TriggerResponse:
    """Resume a paused trigger."""
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.triggers import TriggerService
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = TriggerService(session)
    rec = svc.resume_trigger(context, trigger_id)
    return _to_response(rec)
