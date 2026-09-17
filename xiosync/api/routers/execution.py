"""execution.py — On-demand task execution router.

Allows API callers to directly execute a workflow node / script / DAG action
without going through the full DAG scheduling system.

Use cases:
  - Run a one-off script on a specific runtime
  - Test a single DAG node in isolation
  - Trigger an ad-hoc HTTP request or browser action from the API

Endpoints
---------
POST /api/v1/execution/script        — execute an inline Python/JS script
POST /api/v1/execution/http          — fire an HTTP request action and return result
POST /api/v1/execution/node          — run a single DAG node definition inline
GET  /api/v1/execution/runs          — list recent on-demand execution records
GET  /api/v1/execution/runs/{run_id} — get result of a specific on-demand run
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.api.middleware.db import get_db
from xiosync.api.middleware.rbac import get_org_context, require_capability
from xiosync.domain.context import OrgContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/execution", tags=["Task Execution"])

_MAX_SCRIPT_BYTES   = 64_000   # 64 KB max script size
_MAX_TIMEOUT_SECS   = 300      # 5-minute hard cap on execution timeout
_DEFAULT_TIMEOUT    = 30


# ── Request / Response models ──────────────────────────────────────────────────

class ScriptExecutionRequest(BaseModel):
    """Execute an inline script using the ScriptRunner."""
    language:    str        = Field(default="python", pattern="^(python|javascript|bash)$")
    script:      str        = Field(..., min_length=1, max_length=_MAX_SCRIPT_BYTES)
    params:      dict[str, Any] = {}
    timeout_secs: int       = Field(default=_DEFAULT_TIMEOUT, ge=1, le=_MAX_TIMEOUT_SECS)
    runtime_id:  str | None = None   # optional: pin to specific runtime session

    model_config = ConfigDict(from_attributes=True)


class HttpExecutionRequest(BaseModel):
    """Fire an HTTP request and capture the response."""
    method:       str                = Field(default="GET", pattern="^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$")
    url:          str                = Field(..., min_length=8)
    headers:      dict[str, str]     = {}
    body:         str | None         = None
    timeout_secs: int                = Field(default=15, ge=1, le=60)
    follow_redirects: bool           = True

    model_config = ConfigDict(from_attributes=True)


class NodeExecutionRequest(BaseModel):
    """Execute a single DAG node definition inline (no graph, no scheduling)."""
    action_type:   str              = Field(..., min_length=1)
    action_params: dict[str, Any]   = {}
    intent:        str              = ""
    session_id:    str | None       = None   # browser session to operate on
    timeout_secs:  int              = Field(default=_DEFAULT_TIMEOUT, ge=1, le=_MAX_TIMEOUT_SECS)

    model_config = ConfigDict(from_attributes=True)


class ExecutionResult(BaseModel):
    run_id:      str
    status:      str          # success | error | timeout
    output:      Any          = None
    error:       str | None   = None
    duration_ms: int
    executed_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _record_run(
    db:     Session,
    org_id: uuid.UUID,
    kind:   str,
    request_payload: dict,
    result_payload:  dict,
    duration_ms: int,
    status: str,
) -> str:
    """Persist an on-demand execution record to the events table."""
    run_id = str(uuid.uuid4())
    try:
        db.execute(
            text("""
                INSERT INTO events
                  (id, organization_id, event_type, payload, severity, entity_type, created_at)
                VALUES
                  (cast(:id as uuid), cast(:org as uuid), :etype,
                   cast(:payload as jsonb), :severity, 'on_demand_run', now())
            """),
            {
                "id":       run_id,
                "org":      str(org_id),
                "etype":    f"execution.{kind}.{status}",
                "payload":  json.dumps({
                    "run_id":      run_id,
                    "kind":        kind,
                    "request":     request_payload,
                    "result":      result_payload,
                    "duration_ms": duration_ms,
                    "status":      status,
                }),
                "severity": "info" if status == "success" else "error",
            },
        )
        db.commit()
    except Exception as exc:
        logger.warning("execution.record_failed", extra={"error": str(exc)})
    return run_id


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/script", response_model=ExecutionResult, status_code=200)
def execute_script(
    req: ScriptExecutionRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Execute an inline script via the ScriptRunner and return the output."""
    t0 = time.monotonic()
    status = "error"
    output = None
    error  = None

    try:
        from xiosync.subsystems.xioflow.engine.script_runner import ScriptRunner  # noqa: PLC0415
        runner = ScriptRunner()
        result = runner.run(
            script=req.script,
            language=req.language,
            params=req.params,
            timeout_secs=req.timeout_secs,
        )
        output = result
        status = "success"
    except TimeoutError as exc:
        error  = f"Script timed out after {req.timeout_secs}s"
        status = "timeout"
        logger.warning("execution.script.timeout", extra={"org": str(ctx.organization_id)})
    except Exception as exc:
        error  = str(exc)
        status = "error"
        logger.warning("execution.script.error", extra={"error": error, "org": str(ctx.organization_id)})

    duration_ms = int((time.monotonic() - t0) * 1000)
    run_id = _record_run(
        db, ctx.organization_id, "script",
        {"language": req.language, "timeout_secs": req.timeout_secs},
        {"output": output, "error": error},
        duration_ms, status,
    )
    return {
        "run_id":      run_id,
        "status":      status,
        "output":      output,
        "error":       error,
        "duration_ms": duration_ms,
        "executed_at": datetime.now(timezone.utc),
    }


@router.post("/http", response_model=ExecutionResult, status_code=200)
def execute_http(
    req: HttpExecutionRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Fire an HTTP request and return status code + response body."""
    import urllib.request  # noqa: PLC0415
    import urllib.error    # noqa: PLC0415

    t0     = time.monotonic()
    status = "error"
    output = None
    error  = None

    try:
        request = urllib.request.Request(
            url=req.url,
            method=req.method,
            headers=req.headers,
            data=req.body.encode() if req.body else None,
        )
        with urllib.request.urlopen(request, timeout=req.timeout_secs) as resp:
            body = resp.read(65536).decode("utf-8", errors="replace")
            output = {
                "status_code": resp.status,
                "headers":     dict(resp.headers),
                "body":        body,
            }
        status = "success"
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace") if exc.fp else ""
        output = {"status_code": exc.code, "body": body}
        error  = f"HTTP {exc.code}: {exc.reason}"
        status = "error"
    except TimeoutError:
        error  = f"Request timed out after {req.timeout_secs}s"
        status = "timeout"
    except Exception as exc:
        error  = str(exc)
        status = "error"

    duration_ms = int((time.monotonic() - t0) * 1000)
    run_id = _record_run(
        db, ctx.organization_id, "http",
        {"method": req.method, "url": req.url},
        {"output": output, "error": error},
        duration_ms, status,
    )
    return {
        "run_id":      run_id,
        "status":      status,
        "output":      output,
        "error":       error,
        "duration_ms": duration_ms,
        "executed_at": datetime.now(timezone.utc),
    }


@router.post("/node", response_model=ExecutionResult, status_code=200)
async def execute_node(
    req: NodeExecutionRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Execute a single DAG action node inline (no graph scheduling)."""
    t0     = time.monotonic()
    status = "error"
    output = None
    error  = None

    try:
        from xiosync.subsystems.xioflow.engine.dag_executor import DAGExecutor  # noqa: PLC0415

        executor = DAGExecutor(
            run_id=str(uuid.uuid4()),
            org_id=str(ctx.organization_id),
            session_id=req.session_id or "",
            db=db,
        )
        node = {
            "intent":        req.intent or req.action_type,
            "action_type":   req.action_type,
            "action_params": req.action_params,
            "next":          [],
            "fallback":      None,
        }

        import asyncio  # noqa: PLC0415
        success = await asyncio.wait_for(
            executor._execute_node(node, {}),
            timeout=req.timeout_secs,
        )
        output = {"node_success": success}
        status = "success" if success else "error"
        if not success:
            error = "Node returned failure status"
    except asyncio.TimeoutError:
        error  = f"Node execution timed out after {req.timeout_secs}s"
        status = "timeout"
    except Exception as exc:
        error  = str(exc)
        status = "error"
        logger.warning("execution.node.error", extra={"error": error, "action_type": req.action_type})

    duration_ms = int((time.monotonic() - t0) * 1000)
    run_id = _record_run(
        db, ctx.organization_id, "node",
        {"action_type": req.action_type, "action_params": req.action_params},
        {"output": output, "error": error},
        duration_ms, status,
    )
    return {
        "run_id":      run_id,
        "status":      status,
        "output":      output,
        "error":       error,
        "duration_ms": duration_ms,
        "executed_at": datetime.now(timezone.utc),
    }


@router.get("/runs", response_model=list[ExecutionResult])
def list_execution_runs(
    limit:  int     = 50,
    offset: int     = 0,
    kind:   str     = "",          # 'script' | 'http' | 'node' | '' for all
    db:     Session    = Depends(get_db),
    ctx:    OrgContext = Depends(get_org_context),
) -> list[dict]:
    """List recent on-demand execution records for this org."""
    type_filter = f"AND event_type = 'execution.{kind}.success' OR event_type = 'execution.{kind}.error'" if kind else ""
    rows = db.execute(
        text(f"""
            SELECT id,
                   payload->>'run_id'      as run_id,
                   payload->>'status'      as status,
                   payload->>'duration_ms' as duration_ms,
                   payload->'result'->>'error' as error,
                   created_at
            FROM   events
            WHERE  organization_id = :org
              AND  event_type LIKE 'execution.%'
              {"AND event_type LIKE :kind_filter" if kind else ""}
            ORDER  BY created_at DESC
            LIMIT  :limit OFFSET :offset
        """),
        {
            "org":    str(ctx.organization_id),
            "limit":  min(limit, 200),
            "offset": offset,
            **({"kind_filter": f"execution.{kind}.%"} if kind else {}),
        },
    ).mappings().all()

    return [
        {
            "run_id":      r["run_id"] or str(r["id"]),
            "status":      r["status"] or "unknown",
            "output":      None,
            "error":       r["error"],
            "duration_ms": int(r["duration_ms"] or 0),
            "executed_at": r["created_at"],
        }
        for r in rows
    ]


@router.get("/runs/{run_id}", response_model=ExecutionResult)
def get_execution_run(
    run_id: str,
    db:     Session    = Depends(get_db),
    ctx:    OrgContext = Depends(get_org_context),
) -> dict:
    """Fetch a specific on-demand execution run by its run_id."""
    row = db.execute(
        text("""
            SELECT payload, created_at
            FROM   events
            WHERE  organization_id = :org
              AND  event_type LIKE 'execution.%'
              AND  payload->>'run_id' = :run_id
            ORDER  BY created_at DESC
            LIMIT  1
        """),
        {"org": str(ctx.organization_id), "run_id": run_id},
    ).mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Execution run '{run_id}' not found")

    p = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
    result = p.get("result", {})
    return {
        "run_id":      p.get("run_id", run_id),
        "status":      p.get("status", "unknown"),
        "output":      result.get("output"),
        "error":       result.get("error"),
        "duration_ms": p.get("duration_ms", 0),
        "executed_at": row["created_at"],
    }


# ── Router registration ────────────────────────────────────────────────────────
from xiosync.api.router_registry import register_router  # noqa: E402

register_router(
    router,
    prefix="/api/v1",
    tags=["Task Execution"],
    dependencies=[require_capability("task.execute")],
)
