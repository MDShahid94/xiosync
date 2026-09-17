"""XIOFLOW Events API — DLQ inspection and retry, SSE stream."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow/events", tags=["XIOFLOW Events"])


def _session(request: Request) -> OrmSession:
    return cast(OrmSession, request.state.org_session)


# ── In-process SSE broker ─────────────────────────────────────────────────────
# Maps org_id → set of asyncio.Queue instances (one per connected client).
# The run_dispatcher calls publish_event() after each state change.
# For multi-process deployments, swap _subscribers for Redis pub/sub
# without changing the /stream API surface.

_subscribers: dict[str, set[asyncio.Queue]] = {}  # org_id → queues


def publish_event(org_id: str, event: dict[str, Any]) -> None:
    """Publish a run/task/node event to all SSE subscribers for this org.

    Called from run_dispatcher (sync context) — safe because asyncio.Queue
    is thread-safe for put_nowait().
    """
    queues = _subscribers.get(org_id, set())
    data = json.dumps(event)
    for q in list(queues):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass  # slow client — drop, they'll reconcile via poll


async def _sse_generator(org_id: str, queue: asyncio.Queue):
    """Yield SSE-formatted events until the client disconnects."""
    try:
        yield f'data: {{"type":"connected","org_id":"{org_id}"}}\n\n'
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=30)
                yield f"data: {data}\n\n"
            except asyncio.TimeoutError:
                # Heartbeat keepalive — prevents proxy timeouts
                yield ": keepalive\n\n"
    finally:
        queues = _subscribers.get(org_id)
        if queues:
            queues.discard(queue)
            if not queues:
                _subscribers.pop(org_id, None)


@router.get("/stream", summary="SSE real-time event stream for XIOFLOW runs/tasks")
async def get_event_stream(request: Request) -> StreamingResponse:
    """Subscribe to live run, task, and node events for this org.

    Events emitted:
      {"type":"run.started",    "run_id":"...", "template_type":"..."}
      {"type":"run.completed",  "run_id":"...", "state":"SUCCESS|FAILED"}
      {"type":"run.paused",     "run_id":"..."}
      {"type":"task.claimed",   "task_id":"...", "run_id":"...", "intent":"..."}
      {"type":"task.completed", "task_id":"...", "state":"SUCCESS|FAILED"}
      {"type":"node.success",   "intent":"...", "tier_used":3}
      {"type":"node.healed",    "intent":"...", "new_locator":"..."}
      {"type":"circuit.opened", "domain":"..."}
    """
    from xiosync.domain.context import OrgContext
    ctx = cast(OrgContext, request.state.org_context)
    org_id = str(ctx.organization_id)

    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.setdefault(org_id, set()).add(queue)

    return StreamingResponse(
        _sse_generator(org_id, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Dead Letter Queue ─────────────────────────────────────────────────────────

@router.get("/dlq", summary="List dead-lettered xioflow runs")
def list_dlq(request: Request, limit: int = 50):
    """Return the most recent dead letters with run + task context."""
    session = _session(request)
    rows = session.execute(
        text("""
            SELECT dl.id, dl.run_id, dl.task_id, dl.retry_count,
                   dl.last_error, dl.created_at, dl.resolved_at,
                   r.state   AS run_state,
                   t.template_type, t.script_ref
            FROM   xioflow_dead_letters dl
            JOIN   xioflow_runs r ON r.id = dl.run_id
            LEFT JOIN workflow_templates t ON t.id = r.template_id
            ORDER  BY dl.created_at DESC
            LIMIT  :lim
        """),
        {"lim": limit},
    ).fetchall()

    return {
        "dead_letters": [
            {
                "id": str(row.id),
                "run_id": str(row.run_id),
                "task_id": str(row.task_id) if row.task_id else None,
                "retry_count": row.retry_count,
                "last_error": row.last_error,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                "run_state": row.run_state,
                "template_type": row.template_type,
                "script_ref": row.script_ref,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.post("/dlq/{dead_letter_id}/retry", summary="Re-queue a dead-lettered run")
def retry_dlq(dead_letter_id: uuid.UUID, request: Request):
    """Reset the associated run to PENDING so the dispatcher picks it up again."""
    session = _session(request)

    row = session.execute(
        text("SELECT run_id FROM xioflow_dead_letters WHERE id = :id"),
        {"id": str(dead_letter_id)},
    ).fetchone()

    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="dead_letter_not_found")

    run_id = str(row.run_id)

    # Reset run to PENDING and clear task state so dispatcher re-claims
    session.execute(
        text("UPDATE xioflow_runs SET state='PENDING', finished_at=NULL WHERE id=:id"),
        {"id": run_id},
    )
    session.execute(
        text("UPDATE xioflow_tasks SET state='PENDING', claimed_at=NULL, error=NULL WHERE run_id=:id"),
        {"id": run_id},
    )
    session.execute(
        text("UPDATE xioflow_dead_letters SET resolved_at=now() WHERE id=:id"),
        {"id": str(dead_letter_id)},
    )
    session.commit()

    logger.info("dlq_retry_queued", extra={"dead_letter_id": str(dead_letter_id), "run_id": run_id})
    return {"status": "retry_queued", "run_id": run_id}


@router.get("/runs", summary="List xioflow runs")
def list_runs(
    request: Request,
    state: str | None = None,
    limit: int = 50,
):
    """List xioflow_runs for the org, newest first."""
    session = _session(request)
    from xiosync.domain.context import OrgContext
    ctx = cast(OrgContext, request.state.org_context)

    where = "WHERE r.organization_id = :org_id"
    params: dict = {"org_id": str(ctx.organization_id), "lim": limit}
    if state:
        where += " AND r.state = :state"
        params["state"] = state.upper()

    rows = session.execute(
        text(f"""
            SELECT r.id, r.state, r.started_at, r.finished_at, r.error,
                   t.name AS template_name, t.script_ref
            FROM   xioflow_runs r
            LEFT JOIN workflow_templates t ON t.id = r.template_id
            {where}
            ORDER BY r.started_at DESC
            LIMIT :lim
        """),
        params,
    ).fetchall()

    return {
        "runs": [
            {
                "id": str(row.id),
                "state": row.state,
                "template": row.template_name,
                "script_ref": row.script_ref,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "error": row.error,
            }
            for row in rows
        ]
    }


# ── DAG Run Claim/Complete (for Colab workers polling xioflow_dag runs) ───────

@router.get("/runs/pending-dag", summary="Poll for a PENDING xioflow_dag run to claim")
def claim_pending_dag_run(request: Request) -> dict:
    """Colab workers call this to atomically claim one PENDING xioflow_dag run.

    Uses FOR UPDATE SKIP LOCKED so multiple workers don't race.
    Returns 204 (empty) if no pending runs, or the run details.
    """
    from xiosync.domain.context import OrgContext
    from xiosync.platform.ids import new_id
    session = _session(request)
    ctx = cast(OrgContext, request.state.org_context)

    row = session.execute(
        text("""
            WITH claimed AS (
                SELECT r.id, r.context, t.name AS template_name,
                       t.script_ref, t.dag_domain, t.dag_root_intent
                FROM   xioflow_runs r
                JOIN   workflow_templates t ON t.id = r.template_id
                WHERE  r.state = 'PENDING'
                  AND  r.organization_id = :org
                  AND  t.template_type = 'xioflow_dag'
                ORDER  BY r.started_at
                LIMIT  1
                FOR UPDATE OF r SKIP LOCKED
            )
            UPDATE xioflow_runs
            SET    state = 'RUNNING'
            FROM   claimed
            WHERE  xioflow_runs.id = claimed.id
            RETURNING
                xioflow_runs.id,
                claimed.context,
                claimed.template_name,
                claimed.script_ref,
                claimed.dag_domain,
                claimed.dag_root_intent
        """),
        {"org": str(ctx.organization_id)},
    ).fetchone()

    if not row:
        from fastapi.responses import Response
        return Response(status_code=204)

    # Create task row
    task_id = str(new_id())
    session.execute(
        text("""
            INSERT INTO xioflow_tasks
              (id, run_id, node_intent, state, attempt_count, claimed_at)
            VALUES (:id, :run_id, :intent, 'CLAIMED', 1, now())
        """),
        {"id": task_id, "run_id": str(row.id), "intent": row.template_name or "dag_root"},
    )
    session.commit()

    return {
        "run_id": str(row.id),
        "task_id": task_id,
        "context": row.context or {},
        "template_name": row.template_name,
        "dag_domain": row.dag_domain,
        "dag_root_intent": row.dag_root_intent,
    }


class CompleteRunRequest(BaseModel):
    success: bool
    task_id: str | None = None
    result: dict | None = None
    error: str | None = None

    class Config:
        extra = "allow"


@router.post("/runs/{run_id}/complete", status_code=200,
             summary="Worker reports completion of a RUNNING run")
def complete_run(run_id: uuid.UUID, payload: CompleteRunRequest, request: Request) -> dict:
    """Colab worker calls this after executing a DAG run locally."""
    from xiosync.domain.context import OrgContext
    import json as _json
    success = payload.success
    task_id = payload.task_id
    result = payload.result or {}
    error = payload.error

    session = _session(request)
    ctx = cast(OrgContext, request.state.org_context)
    from datetime import UTC, datetime
    now = datetime.now(UTC)

    run_state = "SUCCESS" if success else "FAILED"

    if task_id:
        session.execute(
            text("""
                UPDATE xioflow_tasks
                SET state=:state, result=cast(:res as jsonb), error=:error, completed_at=:now
                WHERE id=:id
            """),
            {"state": run_state, "res": __import__("json").dumps(result), "error": error, "now": now, "id": task_id},
        )

    session.execute(
        text("""
            UPDATE xioflow_runs
            SET state=:state, finished_at=:now, error=:error
            WHERE id=:id AND organization_id=:org
        """),
        {"state": run_state, "now": now, "error": error, "id": str(run_id), "org": str(ctx.organization_id)},
    )
    session.commit()

    logger.info("run_completed_by_worker",
                extra={"run_id": str(run_id), "state": run_state})
    return {"run_id": str(run_id), "state": run_state}
