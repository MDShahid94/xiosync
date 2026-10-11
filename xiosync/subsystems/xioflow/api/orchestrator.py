"""XIOFlow Orchestrator API — control plane endpoints for org-scoped orchestration.

Routes:
  GET  /orchestrator/status          Queue depth, worker availability, config
  GET  /orchestrator/queue           Detailed queue view by priority band
  GET  /orchestrator/workers         Worker capacity and load
  GET  /orchestrator/sla/stats       Template SLA statistics
  POST /orchestrator/escalations/{run_id}/resolve   Resolve HITL escalation
  POST /orchestrator/runs/{run_id}/cancel           Cancel a specific run
  POST /orchestrator/runs/{run_id}/reprioritize     Change run priority
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow/orchestrator", tags=["xioflow-orchestrator"])


# ── Pydantic Models ──────────────────────────────────────────────────────


class ReprioritizeRequest(BaseModel):
    priority: int = Field(..., ge=0, le=100)


class ResolveEscalationRequest(BaseModel):
    response: str | None = None


# ── Dependencies ─────────────────────────────────────────────────────────


def _get_db():
    from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415

    session = Session(get_engine())
    try:
        yield session
    finally:
        session.close()


def _get_org_id(request) -> str:
    org_id = getattr(request.state, "organization_id", None)
    if not org_id:
        raise HTTPException(status_code=403, detail="No organization context")
    return str(org_id)


# ── Routes ───────────────────────────────────────────────────────────────


@router.get("/status")
async def orchestrator_status(
    request=None,
    db: Session = Depends(_get_db),
):
    """Get full orchestrator status for the current org."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    from xiosync.subsystems.xioflow.engine.orchestrator import (  # noqa: PLC0415
        Orchestrator,
    )

    orch = Orchestrator(session=db)
    return await orch.get_status(org_id)


@router.get("/queue")
async def queue_depth(
    request=None,
    db: Session = Depends(_get_db),
):
    """Get detailed queue depth by priority band."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    from xiosync.subsystems.xioflow.engine.orchestrator import (  # noqa: PLC0415
        Orchestrator,
    )

    orch = Orchestrator(session=db)
    depth = await orch.queue.get_queue_depth(org_id)

    # Also get pending runs detail
    rows = db.execute(
        text(
            "SELECT id, template_id, priority, state, started_at, "
            "  context->>'agent_id' AS agent_id "
            "FROM xioflow_runs "
            "WHERE organization_id = :org_id AND state = 'PENDING' "
            "ORDER BY priority DESC NULLS LAST, started_at ASC "
            "LIMIT 50"
        ),
        {"org_id": org_id},
    ).mappings().all()

    return {
        "depth": depth,
        "pending_runs": [dict(r) for r in rows],
    }


@router.get("/workers")
async def worker_capacity(
    request=None,
    db: Session = Depends(_get_db),
):
    """Get worker capacity and current load."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    from xiosync.subsystems.xioflow.engine.orchestrator import (  # noqa: PLC0415
        Orchestrator,
    )

    orch = Orchestrator(session=db)
    workers = await orch.allocator.get_available_workers(org_id)
    return {"workers": workers, "total": len(workers)}


@router.get("/sla/stats/{template_id}")
async def sla_stats(
    template_id: str,
    request=None,
    db: Session = Depends(_get_db),
):
    """Get SLA statistics for a specific workflow template."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        _get_org_id(request)  # ensure org context exists
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    from xiosync.subsystems.xioflow.engine.orchestrator import (  # noqa: PLC0415
        Orchestrator,
    )

    orch = Orchestrator(session=db)
    return await orch.sla.get_template_sla_stats(template_id)


@router.post("/escalations/{run_id}/resolve")
async def resolve_escalation(
    run_id: str,
    body: ResolveEscalationRequest,
    request=None,
    db: Session = Depends(_get_db),
):
    """Resolve a HITL escalation and resume the run."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    # Verify the run belongs to this org
    row = db.execute(
        text(
            "SELECT id FROM xioflow_runs "
            "WHERE id = :id AND organization_id = :org_id"
        ),
        {"id": run_id, "org_id": org_id},
    ).first()

    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    from xiosync.subsystems.xioflow.engine.orchestrator import (  # noqa: PLC0415
        Orchestrator,
    )

    orch = Orchestrator(session=db)
    resolved = await orch.escalation.resolve_escalation(run_id, body.response)

    if not resolved:
        raise HTTPException(
            status_code=409,
            detail="Run is not in WAITING_HUMAN state",
        )

    return {"resolved": True, "run_id": run_id}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    request=None,
    db: Session = Depends(_get_db),
):
    """Cancel a pending or running run."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    result = db.execute(
        text(
            "UPDATE xioflow_runs "
            "SET state = 'CANCELLED', "
            "    finished_at = now(), "
            "    updated_at = now(), "
            "    context = jsonb_set(COALESCE(context, '{}'), "
            "        '{cancel_reason}', '\"manual\"'::jsonb) "
            "WHERE id = :id AND organization_id = :org_id "
            "  AND state IN ('PENDING', 'CLAIMED', 'RUNNING', 'WAITING_HUMAN')"
        ),
        {"id": run_id, "org_id": org_id},
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(
            status_code=409,
            detail="Run not found or not in a cancellable state",
        )

    logger.info(f"orchestrator.run_cancelled: run_id={run_id}")
    return {"cancelled": True, "run_id": run_id}


@router.post("/runs/{run_id}/reprioritize")
async def reprioritize_run(
    run_id: str,
    body: ReprioritizeRequest,
    request=None,
    db: Session = Depends(_get_db),
):
    """Change the priority of a pending run."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    result = db.execute(
        text(
            "UPDATE xioflow_runs "
            "SET priority = :priority, updated_at = now() "
            "WHERE id = :id AND organization_id = :org_id AND state = 'PENDING'"
        ),
        {"id": run_id, "org_id": org_id, "priority": body.priority},
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(
            status_code=409,
            detail="Run not found or not in PENDING state",
        )

    logger.info(
        f"orchestrator.reprioritized: run_id={run_id} priority={body.priority}"
    )
    return {"reprioritized": True, "run_id": run_id, "priority": body.priority}
