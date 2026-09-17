"""XIOFLOW Triggers API — manage workflow automation triggers.

Trigger types:
  cron   — fires on a cron schedule (processed by ticker worker loop)
  event  — fires when a named event is emitted (processed by event_router loop)

Endpoints:
  POST   /xioflow/triggers              Create a trigger
  GET    /xioflow/triggers              List triggers for this org
  GET    /xioflow/triggers/{id}         Get single trigger with last-fire stats
  PATCH  /xioflow/triggers/{id}         Update schedule / context / enable-disable
  DELETE /xioflow/triggers/{id}         Delete (hard delete — runs are preserved)
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext
from xiosync.platform.ids import new_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow/triggers", tags=["XIOFLOW Triggers"])

_VALID_TRIGGER_TYPES = {"cron", "event"}


def _session(request: Request) -> OrmSession:
    return cast(OrmSession, request.state.org_session)


def _ctx(request: Request) -> OrgContext:
    return cast(OrgContext, request.state.org_context)


def _assert_trigger(session: OrmSession, trigger_id: str, org_id: str) -> Any:
    row = session.execute(
        text("""
            SELECT tr.id, tr.trigger_type, tr.cron_schedule, tr.event_name,
                   tr.enabled, tr.context_defaults, tr.last_fired_at, tr.created_at,
                   tr.template_id,
                   t.name AS template_name, t.template_type
            FROM   xioflow_triggers tr
            LEFT JOIN workflow_templates t ON t.id = tr.template_id
            WHERE  tr.id = :id AND tr.organization_id = :org
        """),
        {"id": trigger_id, "org": org_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="trigger_not_found")
    return row


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "trigger_type": row.trigger_type,
        "cron_schedule": row.cron_schedule,
        "event_name": row.event_name,
        "enabled": row.enabled,
        "context_defaults": row.context_defaults or {},
        "template_id": str(row.template_id) if row.template_id else None,
        "template_name": row.template_name,
        "template_type": row.template_type,
        "last_fired_at": row.last_fired_at.isoformat() if row.last_fired_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ── Request models ─────────────────────────────────────────────────────────────

class CreateTriggerRequest(BaseModel):
    """Create a workflow trigger.

    For trigger_type='cron':   cron_schedule is required (standard 5-field cron)
    For trigger_type='event':  event_name is required (e.g. 'user.created')
    template_id is required for both — it defines which workflow to run.
    """
    trigger_type: str
    template_id: uuid.UUID
    cron_schedule: str | None = None
    event_name: str | None = None
    context_defaults: dict[str, Any] = {}
    enabled: bool = True
    model_config = ConfigDict(from_attributes=True)

    @field_validator("trigger_type")
    @classmethod
    def valid_type(cls, v: str) -> str:
        if v not in _VALID_TRIGGER_TYPES:
            raise ValueError(f"trigger_type must be one of {_VALID_TRIGGER_TYPES}")
        return v

    @field_validator("cron_schedule")
    @classmethod
    def valid_cron(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                from croniter import croniter
                if not croniter.is_valid(v):
                    raise ValueError(f"Invalid cron expression: {v!r}")
            except ImportError:
                pass  # croniter not installed — skip validation
        return v


class UpdateTriggerRequest(BaseModel):
    cron_schedule: str | None = None
    event_name: str | None = None
    context_defaults: dict[str, Any] | None = None
    enabled: bool | None = None
    model_config = ConfigDict(from_attributes=True)

    @field_validator("cron_schedule")
    @classmethod
    def valid_cron(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                from croniter import croniter
                if not croniter.is_valid(v):
                    raise ValueError(f"Invalid cron expression: {v!r}")
            except ImportError:
                pass
        return v


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("", summary="Create a workflow trigger", status_code=201)
def create_trigger(request: Request, body: CreateTriggerRequest) -> dict[str, Any]:
    """Create a cron or event trigger that will automatically dispatch workflow runs."""
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)

    # Validate type-specific fields
    if body.trigger_type == "cron" and not body.cron_schedule:
        raise HTTPException(status_code=422, detail="cron_schedule is required for trigger_type='cron'")
    if body.trigger_type == "event" and not body.event_name:
        raise HTTPException(status_code=422, detail="event_name is required for trigger_type='event'")

    # Verify template exists and is visible to this org
    tmpl = session.execute(
        text("""
            SELECT id FROM workflow_templates
            WHERE  id = :id
              AND  (organization_id = :org OR is_platform_global = true)
        """),
        {"id": str(body.template_id), "org": org_id},
    ).fetchone()
    if not tmpl:
        raise HTTPException(status_code=404, detail="template_not_found")

    trigger_id = str(new_id())
    import json
    session.execute(
        text("""
            INSERT INTO xioflow_triggers
              (id, organization_id, template_id, trigger_type, cron_schedule,
               event_name, enabled, context_defaults, created_at)
            VALUES
              (:id, :org, :tmpl, :ttype, :cron,
               :event_name, :enabled, cast(:ctx as jsonb), now())
        """),
        {
            "id": trigger_id,
            "org": org_id,
            "tmpl": str(body.template_id),
            "ttype": body.trigger_type,
            "cron": body.cron_schedule,
            "event_name": body.event_name,
            "enabled": body.enabled,
            "ctx": json.dumps(body.context_defaults),
        },
    )
    session.commit()
    logger.info(
        "trigger_created",
        extra={"trigger_id": trigger_id, "type": body.trigger_type, "org_id": org_id},
    )
    return {
        "id": trigger_id,
        "trigger_type": body.trigger_type,
        "cron_schedule": body.cron_schedule,
        "event_name": body.event_name,
        "enabled": body.enabled,
        "template_id": str(body.template_id),
    }


@router.get("", summary="List triggers for this org")
def list_triggers(
    request: Request,
    trigger_type: str | None = None,
    enabled: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)

    where = ["tr.organization_id = :org"]
    params: dict[str, Any] = {"org": org_id, "lim": limit, "off": offset}

    if trigger_type:
        where.append("tr.trigger_type = :ttype")
        params["ttype"] = trigger_type
    if enabled is not None:
        where.append("tr.enabled = :enabled")
        params["enabled"] = enabled

    rows = session.execute(
        text(f"""
            SELECT tr.id, tr.trigger_type, tr.cron_schedule, tr.event_name,
                   tr.enabled, tr.context_defaults, tr.last_fired_at, tr.created_at,
                   tr.template_id,
                   t.name AS template_name, t.template_type
            FROM   xioflow_triggers tr
            LEFT JOIN workflow_templates t ON t.id = tr.template_id
            WHERE  {" AND ".join(where)}
            ORDER  BY tr.created_at DESC
            LIMIT  :lim OFFSET :off
        """),
        params,
    ).fetchall()

    return {"triggers": [_row_to_dict(r) for r in rows]}


@router.get("/{trigger_id}", summary="Get trigger detail with recent run stats")
def get_trigger(request: Request, trigger_id: str) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)
    row = _assert_trigger(session, trigger_id, str(ctx.organization_id))

    # Recent runs for this trigger
    runs = session.execute(
        text("""
            SELECT id, state, started_at, finished_at
            FROM   xioflow_runs
            WHERE  trigger_id = :tid
            ORDER  BY started_at DESC
            LIMIT  10
        """),
        {"tid": trigger_id},
    ).fetchall()

    result = _row_to_dict(row)
    result["recent_runs"] = [
        {
            "id": str(r.id),
            "state": r.state,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in runs
    ]
    return result


@router.patch("/{trigger_id}", summary="Update trigger schedule, context, or enabled state")
def update_trigger(
    request: Request,
    trigger_id: str,
    body: UpdateTriggerRequest,
) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)
    _assert_trigger(session, trigger_id, org_id)

    import json
    sets: list[str] = []
    params: dict[str, Any] = {"id": trigger_id}

    if body.cron_schedule is not None:
        sets.append("cron_schedule = :cron")
        params["cron"] = body.cron_schedule
    if body.event_name is not None:
        sets.append("event_name = :event_name")
        params["event_name"] = body.event_name
    if body.context_defaults is not None:
        sets.append("context_defaults = cast(:ctx as jsonb)")
        params["ctx"] = json.dumps(body.context_defaults)
    if body.enabled is not None:
        sets.append("enabled = :enabled")
        params["enabled"] = body.enabled

    if not sets:
        raise HTTPException(status_code=422, detail="No fields to update")

    session.execute(
        text(f"UPDATE xioflow_triggers SET {', '.join(sets)} WHERE id = :id"),
        params,
    )
    session.commit()

    row = _assert_trigger(session, trigger_id, org_id)
    return _row_to_dict(row)


@router.delete("/{trigger_id}", summary="Delete a trigger (runs are preserved)", status_code=204)
def delete_trigger(request: Request, trigger_id: str) -> None:
    session = _session(request)
    ctx = _ctx(request)
    _assert_trigger(session, trigger_id, str(ctx.organization_id))

    session.execute(
        text("DELETE FROM xioflow_triggers WHERE id = :id"),
        {"id": trigger_id},
    )
    session.commit()
    logger.info("trigger_deleted", extra={"trigger_id": trigger_id})
