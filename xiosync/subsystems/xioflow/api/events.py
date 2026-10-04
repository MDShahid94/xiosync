"""XIOFLOW Events API — DLQ inspection and retry, SSE stream."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow/events", tags=["XIOFLOW Events"])

# Separate router for worker-internal endpoints — mounted WITHOUT RBAC in app.py
internal_router = APIRouter(prefix="/xioflow/events", tags=["XIOFLOW Internal"])


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
            except TimeoutError:
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

@internal_router.get("/runs/pending-dag-internal",
            summary="[Worker-internal] Poll for a PENDING xioflow_dag run — no JWT, uses X-XIOSYNC-Internal",
            include_in_schema=True)
def claim_pending_dag_run_internal(request: Request) -> dict:
    """Internal endpoint for Colab workers — authenticated by X-XIOSYNC-Internal header.
    
    Replaces the JWT-authenticated /runs/pending-dag for worker polling.
    Workers use XIORUN_INTERNAL_SECRET as the X-XIOSYNC-Internal header value.
    """
    import os as _os
    from fastapi.responses import Response as _Resp
    from xiosync.platform.ids import new_id
    
    expected = _os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    given = request.headers.get("X-XIOSYNC-Internal", "")
    if not expected or not given or given != expected:
        raise HTTPException(status_code=403, detail="invalid_internal_secret")
    
    # Use a raw DB session — bypass org scoping since this is internal
    from sqlalchemy.orm import Session as _Sess
    from xiosync.platform.engine_ref import get_engine
    from sqlalchemy import text
    from xiosync.platform.ids import new_id
    
    with _Sess(get_engine()) as sess:
        row = sess.execute(text("""
            WITH claimed AS (
                SELECT r.id, r.context, r.organization_id,
                       t.name AS template_name,
                       t.dag_domain, t.dag_root_intent
                FROM   xioflow_runs r
                JOIN   workflow_templates t ON t.id = r.template_id
                WHERE  r.state = 'PENDING'
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
                claimed.organization_id,
                claimed.template_name,
                claimed.dag_domain,
                claimed.dag_root_intent
        """)).fetchone()
        
        if not row:
            return _Resp(status_code=204)
        
        task_id = str(new_id())
        sess.execute(text("""
            INSERT INTO xioflow_tasks
              (id, run_id, node_intent, state, attempt_count, claimed_at)
            VALUES (:id, :run_id, :intent, 'CLAIMED', 1, now())
        """), {"id": task_id, "run_id": str(row.id), "intent": row.template_name or "dag_root"})
        sess.commit()
    
    return {
        "run_id": str(row.id),
        "task_id": task_id,
        "organization_id": str(row.organization_id),
        "context": row.context or {},
        "template_name": row.template_name,
        "dag_domain": row.dag_domain,
        "dag_root_intent": row.dag_root_intent,
    }

@internal_router.post("/runs-internal/{run_id}/complete", status_code=200,
             summary="[Worker-internal] Report DAG run completion — no JWT")
def complete_run_internal(run_id: uuid.UUID, payload: CompleteRunRequest, request: Request) -> dict:
    """Internal version of complete — used by Colab worker polling loop."""
    import os as _os
    import json
    expected = _os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    given = request.headers.get("X-XIOSYNC-Internal", "")
    if not expected or not given or given != expected:
        raise HTTPException(status_code=403, detail="invalid_internal_secret")
    
    from sqlalchemy.orm import Session as _Sess
    from xiosync.platform.engine_ref import get_engine
    from sqlalchemy import text
    from datetime import UTC, datetime
    
    run_state = "SUCCESS" if payload.success else "FAILED"
    now = datetime.now(UTC)
    
    with _Sess(get_engine()) as sess:
        if payload.task_id:
            task_state = "SUCCESS" if payload.success else "FAILED"
            sess.execute(text("""
                UPDATE xioflow_tasks SET state = :s, completed_at = :now, error = :error, result = cast(:res as jsonb)
                WHERE id = :tid
            """), {"s": task_state, "now": now, "tid": payload.task_id, "error": payload.error, "res": json.dumps(payload.result or {})})
        
        sess.execute(text("""
            UPDATE xioflow_runs
            SET state = :s,
                finished_at = :now,
                error = :error
            WHERE id = :rid
        """), {
            "s": run_state, "now": now,
            "error": payload.error,
            "rid": str(run_id),
        })
        sess.commit()
    
    return {"run_id": str(run_id), "state": run_state}

@internal_router.get("/memory-graph-internal", summary="[Worker-internal] Get DAG graph for execution")
def get_memory_graph_internal(request: Request, domain: str, intent: str) -> dict:
    import os as _os
    expected = _os.environ.get('XIOSYNC_INTERNAL_SECRET', '')
    given = request.headers.get('X-XIOSYNC-Internal', '')
    if not expected or given != expected:
        raise HTTPException(status_code=403, detail='invalid_internal_secret')
    
    from sqlalchemy.orm import Session as _Sess
    from xiosync.platform.engine_ref import get_engine
    from sqlalchemy import text
    
    with _Sess(get_engine()) as sess:
        # ── Step 1: collect script nodes (phase pipeline) — run BEFORE main DAG ──
        # Script nodes are not connected via next_intents, so BFS misses them.
        # We prepend them sorted by phase order so cascade/UC-login run first.
        _PHASE_ORDER = {
            "resolve_exit_proxy": 0,
            "phase0_cascade_check": 1,
            "phase0_browser_check": 2,
            "phase1_uc_stealth_login": 3,
            "phase2_navigate_to_gmail": 4,
            "phase3_verify": 5,
            "phase3_5_persist_session": 6,
        }
        script_rows = sess.execute(text("""
            SELECT DISTINCT ON (intent)
                intent, action_type, action_params, place_value, face_value, locator_priority
            FROM xioflow_memory_nodes
            WHERE domain = :domain AND status = 'ACTIVE' AND action_type = 'script'
            ORDER BY intent, tier DESC
        """), {'domain': domain}).fetchall()

        script_nodes = sorted(
            [
                {
                    'intent': r.intent,
                    'action_type': r.action_type,
                    'action_params': r.action_params or {},
                    'place_value': r.place_value or {},
                    'face_value': r.face_value or {},
                    'locator_priority': r.locator_priority or [6, 1, 2, 3, 4, 5],
                }
                for r in script_rows
            ],
            key=lambda n: _PHASE_ORDER.get(n['intent'], 99),
        )
        script_intents = {n['intent'] for n in script_nodes}

        # ── Step 2: BFS from root intent for navigate/fill/click/wait nodes ──
        visited = set(script_intents)   # skip script intents already included
        queue = [intent]
        dag_nodes = []
        while queue:
            current_intent = queue.pop(0)
            if current_intent in visited:
                continue
            visited.add(current_intent)
            row = sess.execute(text("""
                SELECT intent, action_type, action_params, place_value, face_value,
                       locator_priority, status
                FROM xioflow_memory_nodes
                WHERE domain = :domain AND intent = :intent AND status = 'ACTIVE'
                  AND action_type NOT IN ('done', 'extract_data', 'script')
                ORDER BY tier DESC LIMIT 1
            """), {'domain': domain, 'intent': current_intent}).fetchone()
            if not row:
                continue
            dag_nodes.append({
                'intent': row.intent,
                'action_type': row.action_type,
                'action_params': row.action_params or {},
                'place_value': row.place_value or {},
                'face_value': row.face_value or {},
                'locator_priority': row.locator_priority or [6, 1, 2, 3, 4, 5],
            })
            next_intents = (row.action_params or {}).get('next_intents', [])
            queue.extend(next_intents)

        # Script nodes first (phase pipeline), then BFS DAG nodes
        nodes = script_nodes + dag_nodes
        return {'domain': domain, 'root_intent': intent, 'nodes': nodes,
                'script_count': len(script_nodes), 'dag_count': len(dag_nodes)}

@internal_router.post("/trace-nodes-internal", status_code=200,
                      summary="[Worker-internal] Bulk-deploy auto-traced steps as memory nodes")
def deploy_trace_nodes_internal(request: Request, payload: dict) -> dict:
    """Worker posts executed steps after a trace_mode=True run.
    
    Payload:
        org_id: str
        domain: str
        nodes: list of {intent, action_type, place_value, action_params, previous_intent?}
    """
    import os as _os, json as _json
    from sqlalchemy.orm import Session as _Sess
    from xiosync.platform.engine_ref import get_engine
    from xiosync.platform.ids import new_id

    expected = _os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    given = request.headers.get("X-XIOSYNC-Internal", "")
    if not expected or given != expected:
        raise HTTPException(status_code=403, detail="invalid_internal_secret")

    org_id   = payload.get("org_id", "")
    domain   = payload.get("domain", "")
    nodes    = payload.get("nodes", [])
    run_id   = payload.get("run_id", "")

    if not org_id or not domain or not nodes:
        raise HTTPException(status_code=400, detail="org_id, domain, nodes required")

    inserted = 0
    with _Sess(get_engine()) as sess:
        for i, node in enumerate(nodes):
            intent       = node.get("intent", "")
            action_type  = node.get("action_type", "")
            place_value  = node.get("place_value", {})
            action_params = node.get("action_params", {})
            prev_intent  = node.get("previous_intent")
            if not intent or not action_type:
                continue
            sess.execute(text("""
                INSERT INTO xioflow_memory_nodes
                  (id, organization_id, domain, intent, action_type, action_params,
                   place_value, face_value, tier, status, recording_method,
                   context_hash, previous_intent, created_at)
                VALUES
                  (:id, :org, :domain, :intent, :action_type, cast(:params as jsonb),
                   cast(:place as jsonb), '{}'::jsonb, 'project_experimental', 'ACTIVE',
                   'auto_trace', 'default', :prev, now())
                ON CONFLICT DO NOTHING
            """), {
                "id": str(new_id()),
                "org": org_id,
                "domain": domain,
                "intent": intent,
                "action_type": action_type,
                "params": _json.dumps(action_params),
                "place": _json.dumps(place_value),
                "prev": prev_intent,
            })
            inserted += 1
        sess.commit()

    logger.info("trace_nodes.deployed",
                extra={"org_id": org_id, "domain": domain, "run_id": run_id, "count": inserted})
    return {"deployed": inserted, "domain": domain, "run_id": run_id}


class HitlPauseRequest(BaseModel):
    challenge_type: str
    novnc_url: str
    message: str
    current_url: str

@internal_router.post("/runs-internal/{run_id}/hitl-pause", status_code=200,
                      summary="[Worker-internal] Pause DAG run for HITL")
def hitl_pause_internal(run_id: uuid.UUID, payload: HitlPauseRequest, request: Request) -> dict:
    import os as _os, json as _json
    from sqlalchemy.orm import Session as _Sess
    from xiosync.platform.engine_ref import get_engine

    expected = _os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    given = request.headers.get("X-XIOSYNC-Internal", "")
    if not expected or given != expected:
        raise HTTPException(status_code=403, detail="invalid_internal_secret")

    with _Sess(get_engine()) as sess:
        sess.execute(text("""
            UPDATE xioflow_runs
            SET state = 'PAUSED',
                context = jsonb_set(COALESCE(context, '{}'::jsonb), '{hitl}', cast(:hitl as jsonb))
            WHERE id = :rid
        """), {
            "hitl": _json.dumps(payload.model_dump()),
            "rid": str(run_id)
        })
        sess.commit()
    logger.info("hitl.paused", extra={"run_id": str(run_id), "challenge": payload.challenge_type})
    return {"status": "paused"}


@internal_router.get("/runs-internal/{run_id}/hitl-status", status_code=200,
                     summary="[Worker-internal] Check if HITL is resolved")
def hitl_status_internal(run_id: uuid.UUID, request: Request) -> dict:
    import os as _os
    from sqlalchemy.orm import Session as _Sess
    from xiosync.platform.engine_ref import get_engine

    expected = _os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    given = request.headers.get("X-XIOSYNC-Internal", "")
    if not expected or given != expected:
        raise HTTPException(status_code=403, detail="invalid_internal_secret")

    with _Sess(get_engine()) as sess:
        row = sess.execute(text("""
            SELECT state FROM xioflow_runs WHERE id = :rid
        """), {"rid": str(run_id)}).fetchone()
        
        resumed = row and row.state == 'RUNNING'
        return {"hitl_resumed": resumed}


@internal_router.post("/runs-internal/{run_id}/hitl-resume", status_code=200,
                      summary="[Worker-internal] Resume DAG run from HITL")
def hitl_resume_internal(run_id: uuid.UUID, request: Request) -> dict:
    import os as _os
    from sqlalchemy.orm import Session as _Sess
    from xiosync.platform.engine_ref import get_engine

    expected = _os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    given = request.headers.get("X-XIOSYNC-Internal", "")
    if not expected or given != expected:
        raise HTTPException(status_code=403, detail="invalid_internal_secret")

    with _Sess(get_engine()) as sess:
        sess.execute(text("""
            UPDATE xioflow_runs
            SET state = 'RUNNING'
            WHERE id = :rid AND state = 'PAUSED'
        """), {"rid": str(run_id)})
        sess.commit()
    logger.info("hitl.resumed", extra={"run_id": str(run_id)})
    return {"status": "resumed"}


@internal_router.post("/runs-internal/{run_id}/persist-session", status_code=200,
                      summary="[Worker-internal] Persist cookie vault + identity update after DAG login")
def persist_session_internal(run_id: uuid.UUID, payload: dict, request: Request) -> dict:
    """Save the Patchright storage_state (cookies) into vaulted_secrets via SessionStateIO.

    Flow:
      1. Resolve identity_id: use payload["identity_id"] if given, else look up
         identities WHERE identifier=email AND platform='google'. Create if missing.
      2. Call SessionStateIO.save() — AES-GCM encrypt, TTL-aware merge, upsert.
      3. Update identity.last_used_at = now().
      4. Return {ok, identity_id, profile_serial, cookie_count}.
    """
    import os as _os, datetime as _dt
    from sqlalchemy.orm import Session as _Sess
    from xiosync.platform.engine_ref import get_engine
    from xiosync.subsystems.xiorun.session_state import SessionStateIO

    expected = _os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    given    = request.headers.get("X-XIOSYNC-Internal", "")
    if not expected or given != expected:
        raise HTTPException(status_code=403, detail="invalid_internal_secret")

    storage_state = payload.get("storage_state", {})
    email         = payload.get("email", "")
    identity_id   = payload.get("identity_id", "")
    org_id        = payload.get("org_id", "00000000-0000-7000-8000-000000000000")

    engine = get_engine()

    with _Sess(engine) as sess:
        # ── 1. Resolve / create identity ─────────────────────────────────
        if not identity_id and email:
            row = sess.execute(text("""
                SELECT id, profile_serial FROM identities
                WHERE identifier = :email AND platform = 'google'
                LIMIT 1
            """), {"email": email}).fetchone()

            if row:
                identity_id    = str(row[0])
                profile_serial = row[1] or 0
            else:
                # Create a new identity for this email
                new_id = uuid.uuid4()
                # Next profile_serial = MAX(profile_serial) + 1
                max_row = sess.execute(text(
                    "SELECT COALESCE(MAX(profile_serial), 0) FROM identities "
                    "WHERE organization_id = :org"
                ), {"org": org_id}).fetchone()
                profile_serial = (max_row[0] or 0) + 1
                sess.execute(text("""
                    INSERT INTO identities
                        (id, organization_id, identifier, platform, display_name,
                         state, metadata, profile_serial, materialization_mode, created_at, updated_at)
                    VALUES
                        (:id, :org, :email, 'google', :name,
                         'active', '{}', :serial, 'storage_state', now(), now())
                """), {"id": str(new_id), "org": org_id, "email": email,
                       "name": email.split("@")[0], "serial": profile_serial})
                sess.commit()
                identity_id = str(new_id)
                logger.info("persist_session.identity_created",
                            extra={"identity_id": identity_id, "email": email, "serial": profile_serial})
        elif identity_id:
            row = sess.execute(text(
                "SELECT profile_serial FROM identities WHERE id = :iid"
            ), {"iid": identity_id}).fetchone()
            profile_serial = (row[0] if row else 0) or 0
        else:
            raise HTTPException(status_code=400, detail="email or identity_id required")

        # ── 2. Save cookies to vault via SessionStateIO (AES-GCM + merge) ─
        sio = SessionStateIO(engine)
        sio.save(
            identity_id = identity_id,
            org_id      = org_id,
            state       = storage_state,
            page_url    = "https://myaccount.google.com/",
        )

        # ── 3. Update identity.last_used_at ───────────────────────────────
        sess.execute(text("""
            UPDATE identities SET last_used_at = now(), updated_at = now()
            WHERE id = :iid
        """), {"iid": identity_id})
        sess.commit()

    cookie_count = len(storage_state.get("cookies", []))
    logger.info("persist_session.done", extra={
        "run_id":       str(run_id),
        "identity_id":  identity_id,
        "cookie_count": cookie_count,
        "profile_serial": profile_serial,
    })
    return {
        "ok":             True,
        "identity_id":    identity_id,
        "profile_serial": profile_serial,
        "cookie_count":   cookie_count,
    }

# ── P0-2 helper: identity resolve (internal, no JWT) ─────────────────────────
@internal_router.get(
    "/identity-internal",
    summary="[Worker-internal] Resolve identity_id + profile_serial from email",
)
def resolve_identity_internal(
    request: Request,
    email: str,
    platform: str = "google",
) -> dict:
    """Worker calls this before browser launch to get profile_serial for identity-scoped paths."""
    import os as _os
    _exp = _os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    _given = request.headers.get("X-XIOSYNC-Internal", "")
    if not _exp or _given != _exp:
        raise HTTPException(status_code=403, detail="invalid_internal_secret")
    from sqlalchemy.orm import Session as _Sess
    from xiosync.platform.engine_ref import get_engine
    from sqlalchemy import text as _t

    with _Sess(get_engine()) as sess:
        row = sess.execute(
            _t(
                "SELECT id, profile_serial FROM identities "
                "WHERE identifier = :email AND platform = :platform LIMIT 1"
            ),
            {"email": email, "platform": platform},
        ).fetchone()

    if not row:
        return {"found": False, "identity_id": None, "profile_serial": 0}

    return {
        "found": True,
        "identity_id": str(row[0]),
        "profile_serial": int(row[1] or 0),
    }
