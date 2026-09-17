"""XIOFLOW Runs API — dispatch, track, and control workflow run lifecycle.

Endpoints:
  POST   /xioflow/runs                     Dispatch a new run
  GET    /xioflow/runs                     List runs (with state/template filters)
  GET    /xioflow/runs/{id}                Run detail + task breakdown
  POST   /xioflow/runs/{id}/pause          HITL pause — set state=PAUSED
  POST   /xioflow/runs/{id}/resume         Resume paused run → PENDING
  POST   /xioflow/runs/{id}/cancel         Abort run → CANCELLED
  GET    /xioflow/stats                    Aggregate dashboard stats
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext
from xiosync.platform.ids import new_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow", tags=["XIOFLOW Runs"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _session(request: Request) -> OrmSession:
    return cast(OrmSession, request.state.org_session)


def _ctx(request: Request) -> OrgContext:
    return cast(OrgContext, request.state.org_context)


def _assert_run(session: OrmSession, run_id: str, org_id: str) -> Any:
    row = session.execute(
        text("""
            SELECT r.id, r.state, r.template_id, r.trigger_id,
                   r.context, r.started_at, r.finished_at,
                   t.template_type, t.name AS template_name,
                   t.script_ref, t.dag_domain, t.dag_root_intent
            FROM   xioflow_runs r
            LEFT JOIN workflow_templates t ON t.id = r.template_id
            WHERE  r.id = :id AND r.organization_id = :org
        """),
        {"id": run_id, "org": org_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="run_not_found")
    return row


# ── Request / response models ─────────────────────────────────────────────────

class DispatchRunRequest(BaseModel):
    """Manually dispatch a workflow run.

    Either template_id OR (dag_domain + dag_root_intent) must be provided.
    template_id takes precedence if both are given.
    """
    template_id: uuid.UUID | None = None
    dag_domain: str | None = None
    dag_root_intent: str | None = None
    context: dict[str, Any] = {}
    priority: int = 0
    model_config = ConfigDict(from_attributes=True)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/runs", summary="Dispatch a new workflow run", status_code=201)
def dispatch_run(request: Request, body: DispatchRunRequest) -> dict[str, Any]:
    """Create a PENDING xioflow_run — worker will pick it up within one tick."""
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)

    if not body.template_id and not (body.dag_domain and body.dag_root_intent):
        raise HTTPException(
            status_code=422,
            detail="Provide template_id or both dag_domain and dag_root_intent",
        )

    # Resolve template_id from dag_domain/dag_root_intent if not given directly
    template_id: str | None = str(body.template_id) if body.template_id else None
    if not template_id and body.dag_domain and body.dag_root_intent:
        tmpl = session.execute(
            text("""
                SELECT id FROM workflow_templates
                WHERE  dag_domain = :domain
                  AND  dag_root_intent = :intent
                  AND  template_type = 'xioflow_dag'
                  AND  (organization_id = :org OR is_platform_global = true)
                ORDER BY is_platform_global ASC
                LIMIT 1
            """),
            {"domain": body.dag_domain, "intent": body.dag_root_intent, "org": org_id},
        ).fetchone()
        if tmpl:
            template_id = str(tmpl.id)

    run_id = str(new_id())
    session.execute(
        text("""
            INSERT INTO xioflow_runs
              (id, organization_id, template_id, state, context, started_at)
            VALUES
              (:id, :org, :tmpl, 'PENDING', cast(:ctx as jsonb), now())
        """),
        {
            "id": run_id,
            "org": org_id,
            "tmpl": template_id,
            "ctx": json.dumps(body.context),
        },
    )
    session.commit()
    logger.info("runs_api.dispatched", extra={"run_id": run_id, "org_id": org_id})
    return {"id": run_id, "state": "PENDING"}


@router.get("/runs", summary="List workflow runs")
def list_runs(
    request: Request,
    state: str | None = None,
    template_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)

    where = ["r.organization_id = :org"]
    params: dict[str, Any] = {"org": str(ctx.organization_id), "lim": limit, "off": offset}

    if state:
        where.append("r.state = :state")
        params["state"] = state.upper()
    if template_id:
        where.append("r.template_id = :tmpl")
        params["tmpl"] = template_id

    rows = session.execute(
        text(f"""
            SELECT r.id, r.state, r.started_at, r.finished_at,
                   t.name AS template_name, t.template_type
            FROM   xioflow_runs r
            LEFT JOIN workflow_templates t ON t.id = r.template_id
            WHERE  {" AND ".join(where)}
            ORDER BY r.started_at DESC
            LIMIT  :lim OFFSET :off
        """),
        params,
    ).fetchall()

    return {
        "runs": [
            {
                "id": str(row.id),
                "state": row.state,
                "template_name": row.template_name,
                "template_type": row.template_type,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            }
            for row in rows
        ]
    }


@router.get("/runs/{run_id}", summary="Get run detail with tasks")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)

    run = _assert_run(session, run_id, org_id)

    tasks = session.execute(
        text("""
            SELECT id, node_intent, state, attempt_count,
                   result, error, claimed_at, completed_at,
                   retry_count, priority
            FROM   xioflow_tasks
            WHERE  run_id = :run_id
            ORDER  BY claimed_at ASC NULLS LAST
        """),
        {"run_id": run_id},
    ).fetchall()

    return {
        "id": str(run.id),
        "state": run.state,
        "template_type": run.template_type,
        "template_name": run.template_name,
        "script_ref": run.script_ref,
        "dag_domain": run.dag_domain,
        "dag_root_intent": run.dag_root_intent,
        "context": run.context or {},
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "tasks": [
            {
                "id": str(t.id),
                "node_intent": t.node_intent,
                "state": t.state,
                "attempt_count": t.attempt_count,
                "retry_count": t.retry_count,
                "priority": t.priority,
                "result": t.result,
                "error": t.error,
                "claimed_at": t.claimed_at.isoformat() if t.claimed_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in tasks
        ],
    }


@router.post("/runs/{run_id}/pause", summary="Pause a running workflow (HITL)")
def pause_run(request: Request, run_id: str) -> dict[str, Any]:
    """Transition a RUNNING run to PAUSED for human-in-the-loop intervention."""
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)

    run = _assert_run(session, run_id, org_id)
    if run.state not in ("RUNNING", "PENDING"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot pause a run in state {run.state!r}",
        )

    session.execute(
        text("UPDATE xioflow_runs SET state='PAUSED' WHERE id=:id"),
        {"id": run_id},
    )
    session.commit()
    return {"id": run_id, "state": "PAUSED"}


@router.post("/runs/{run_id}/resume", summary="Resume a paused workflow")
def resume_run(request: Request, run_id: str) -> dict[str, Any]:
    """Re-enqueue a PAUSED run as PENDING so the worker picks it up."""
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)

    run = _assert_run(session, run_id, org_id)
    if run.state != "PAUSED":
        raise HTTPException(
            status_code=409,
            detail=f"Can only resume a PAUSED run, got {run.state!r}",
        )

    session.execute(
        text("UPDATE xioflow_runs SET state='PENDING', finished_at=NULL WHERE id=:id"),
        {"id": run_id},
    )
    session.commit()
    return {"id": run_id, "state": "PENDING"}


@router.post("/runs/{run_id}/cancel", summary="Cancel a workflow run")
def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)

    run = _assert_run(session, run_id, org_id)
    if run.state in ("SUCCESS", "FAILED", "CANCELLED"):
        raise HTTPException(
            status_code=409,
            detail=f"Run is already in terminal state {run.state!r}",
        )

    session.execute(
        text("""
            UPDATE xioflow_runs
            SET    state='CANCELLED', finished_at=now()
            WHERE  id=:id
        """),
        {"id": run_id},
    )
    session.commit()
    return {"id": run_id, "state": "CANCELLED"}


@router.get("/stats", summary="XIOFLOW aggregate stats")
def get_stats(request: Request) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)
    org_id = str(ctx.organization_id)

    rows = session.execute(
        text("""
            SELECT state, count(*) AS cnt
            FROM   xioflow_runs
            WHERE  organization_id = :org
            GROUP  BY state
        """),
        {"org": org_id},
    ).fetchall()

    run_counts = {row.state: row.cnt for row in rows}

    dlq_count = session.execute(
        text("""
            SELECT count(*) FROM xioflow_dead_letters dl
            JOIN   xioflow_runs r ON r.id = dl.run_id
            WHERE  r.organization_id = :org
              AND  (dl.resolved IS NULL OR dl.resolved = false)
        """),
        {"org": org_id},
    ).scalar() or 0

    memory_count = session.execute(
        text("""
            SELECT count(*) FROM xioflow_memory_nodes
            WHERE  organization_id = :org AND status = 'ACTIVE'
        """),
        {"org": org_id},
    ).scalar() or 0

    template_count = session.execute(
        text("""
            SELECT count(*) FROM workflow_templates
            WHERE  (organization_id = :org OR is_platform_global = true)
        """),
        {"org": org_id},
    ).scalar() or 0

    return {
        "runs": run_counts,
        "dead_letters_unresolved": dlq_count,
        "memory_nodes_active": memory_count,
        "workflow_templates": template_count,
    }
