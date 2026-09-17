"""XIOFLOW Trigger API — CRUD for xioflow_triggers.

Replaces the legacy triggers router (which used workflow_id + packed config JSONB
pointing at a non-existent workflow_triggers table) with XIOFLOW-native fields
that map directly to what xioflow_triggers table and the ticker loop expect.

Endpoints:
    POST   /triggers                        — create cron or event trigger
    GET    /triggers                        — list (filter by type/enabled/template)
    GET    /triggers/{id}                   — get single trigger
    POST   /triggers/{id}/enable            — enable
    POST   /triggers/{id}/disable           — disable
    POST   /triggers/{id}/fire              — manual immediate fire → PENDING run
    DELETE /triggers/{id}                   — delete
"""
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["triggers"])


# ── Request / Response models ─────────────────────────────────────────────────

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTriggerRequest(_S):
    """Create a cron or event trigger linked to a workflow template."""
    template_id: uuid.UUID = Field(description="ID of the WorkflowTemplate to run")
    trigger_type: str = Field(description="'cron' or 'event'")
    cron_schedule: str | None = Field(
        default=None,
        description="Cron expression (required for trigger_type='cron'). E.g. '0 */6 * * *'"
    )
    event_name: str | None = Field(
        default=None,
        description="Event topic to react to (required for trigger_type='event')"
    )
    context_defaults: dict[str, Any] = Field(
        default_factory=dict,
        description="Default context merged into each run's context at fire time"
    )
    enabled: bool = True


class FireTriggerRequest(_S):
    extra_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra context merged on top of context_defaults for this one run"
    )


class TriggerResponse(_S):
    model_config = ConfigDict(extra="ignore")  # allow extra from DB rows

    id: uuid.UUID
    organization_id: uuid.UUID
    template_id: uuid.UUID
    trigger_type: str
    cron_schedule: str | None = None
    event_name: str | None = None
    enabled: bool
    context_defaults: dict[str, Any]
    last_fired_at: str | None = None
    created_at: str


def _to_resp(rec: Any) -> TriggerResponse:
    return TriggerResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        template_id=rec.template_id,
        trigger_type=rec.trigger_type,
        cron_schedule=rec.cron_schedule,
        event_name=rec.event_name,
        enabled=rec.enabled,
        context_defaults=rec.context_defaults,
        last_fired_at=rec.last_fired_at.isoformat() if rec.last_fired_at else None,
        created_at=rec.created_at.isoformat(),
    )


def _svc(request: Request):
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.triggers import XioflowTriggerService
    session = cast(OrmSession, request.state.org_session)
    return XioflowTriggerService(session)


def _ctx(request: Request):
    from xiosync.domain.context import OrgContext
    return cast(OrgContext, request.state.org_context)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/triggers", status_code=201, response_model=TriggerResponse,
             summary="Create a cron or event trigger for a workflow template")
def create_trigger(payload: CreateTriggerRequest, request: Request) -> TriggerResponse:
    svc = _svc(request)
    try:
        rec = svc.create_trigger(
            _ctx(request),
            template_id=payload.template_id,
            trigger_type=payload.trigger_type,
            cron_schedule=payload.cron_schedule,
            event_name=payload.event_name,
            context_defaults=payload.context_defaults,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _to_resp(rec)


@router.get("/triggers", response_model=list[TriggerResponse],
            summary="List triggers (optional filter by type / enabled / template)")
def list_triggers(
    request: Request,
    trigger_type: str | None = None,
    enabled: bool | None = None,
    template_id: uuid.UUID | None = None,
) -> list[TriggerResponse]:
    recs = _svc(request).list_triggers(
        _ctx(request),
        trigger_type=trigger_type,
        enabled=enabled,
        template_id=template_id,
    )
    return [_to_resp(r) for r in recs]


@router.get("/triggers/{trigger_id}", response_model=TriggerResponse,
            summary="Get a single trigger")
def get_trigger(trigger_id: uuid.UUID, request: Request) -> TriggerResponse:
    from xiosync.services.triggers import TriggerNotFoundError
    try:
        rec = _svc(request).get_trigger(_ctx(request), trigger_id)
    except TriggerNotFoundError:
        raise HTTPException(status_code=404, detail="trigger_not_found")
    return _to_resp(rec)


@router.post("/triggers/{trigger_id}/enable", response_model=TriggerResponse,
             summary="Enable a trigger")
def enable_trigger(trigger_id: uuid.UUID, request: Request) -> TriggerResponse:
    from xiosync.services.triggers import TriggerNotFoundError
    try:
        return _to_resp(_svc(request).enable_trigger(_ctx(request), trigger_id))
    except TriggerNotFoundError:
        raise HTTPException(status_code=404, detail="trigger_not_found")


@router.post("/triggers/{trigger_id}/disable", response_model=TriggerResponse,
             summary="Disable a trigger without deleting it")
def disable_trigger(trigger_id: uuid.UUID, request: Request) -> TriggerResponse:
    from xiosync.services.triggers import TriggerNotFoundError
    try:
        return _to_resp(_svc(request).disable_trigger(_ctx(request), trigger_id))
    except TriggerNotFoundError:
        raise HTTPException(status_code=404, detail="trigger_not_found")


@router.post("/triggers/{trigger_id}/fire", status_code=202,
             summary="Manually fire a trigger — enqueues an immediate PENDING run")
def fire_trigger(
    trigger_id: uuid.UUID,
    payload: FireTriggerRequest,
    request: Request,
) -> dict[str, str]:
    from xiosync.services.triggers import TriggerNotFoundError
    try:
        run_id = _svc(request).fire_now(
            _ctx(request), trigger_id, extra_context=payload.extra_context
        )
    except TriggerNotFoundError:
        raise HTTPException(status_code=404, detail="trigger_not_found")
    return {"run_id": run_id, "state": "PENDING", "message": "run enqueued"}


@router.delete("/triggers/{trigger_id}", status_code=204,
               summary="Permanently delete a trigger")
def delete_trigger(trigger_id: uuid.UUID, request: Request) -> None:
    from xiosync.services.triggers import TriggerNotFoundError
    try:
        _svc(request).delete_trigger(_ctx(request), trigger_id)
    except TriggerNotFoundError:
        raise HTTPException(status_code=404, detail="trigger_not_found")


# ── Legacy pause/resume aliases (map to disable/enable) ──────────────────────

@router.put("/triggers/{trigger_id}/pause", response_model=TriggerResponse,
            summary="Pause a trigger (alias for disable)")
def pause_trigger(trigger_id: uuid.UUID, request: Request) -> TriggerResponse:
    return disable_trigger(trigger_id, request)


@router.put("/triggers/{trigger_id}/resume", response_model=TriggerResponse,
            summary="Resume a paused trigger (alias for enable)")
def resume_trigger(trigger_id: uuid.UUID, request: Request) -> TriggerResponse:
    return enable_trigger(trigger_id, request)
