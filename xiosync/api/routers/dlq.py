import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.api.middleware.db import get_db
from xiosync.api.middleware.rbac import get_org_context, require_capability
from xiosync.api.router_registry import register_router
from xiosync.domain.context import OrgContext

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/dlq/webhooks")
def list_dlq_webhooks(
    limit: int = 50,
    offset: int = 0,
    max_attempts: int = 5,
    db: Session = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
):
    query = text("""
        SELECT e.id as dispatch_event_id,
               e.payload->>'target_url' as target_url,
               e.payload->>'subscription_id' as subscription_id,
               e.created_at,
               COUNT(f.id) as failed_count,
               MAX(f.payload->>'error') as last_error,
               MAX(f.created_at) as last_failed_at
        FROM events e
        JOIN events f ON f.event_type = 'webhook.failed'
                     AND f.payload->>'dispatch_event_id' = e.id::text
                     AND f.organization_id = :org
        WHERE e.event_type = 'webhook.dispatch'
          AND e.organization_id = :org
          AND NOT EXISTS (
              SELECT 1 FROM events d
              WHERE d.event_type = 'webhook.delivered'
                AND d.payload->>'dispatch_event_id' = e.id::text
          )
        GROUP BY e.id, e.payload, e.created_at
        HAVING COUNT(f.id) >= :max_attempts
        ORDER BY e.created_at DESC
        LIMIT :limit OFFSET :offset
    """)
    rows = (
        db.execute(
            query,
            {
                "org": str(ctx.organization_id),
                "max_attempts": max_attempts,
                "limit": limit,
                "offset": offset,
            },
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.get("/dlq/webhooks/{id}")
def get_dlq_webhook(
    id: uuid.UUID,
    db: Session = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
):
    query = text("""
        SELECT e.id as dispatch_event_id,
               e.payload->>'target_url' as target_url,
               e.payload->>'subscription_id' as subscription_id,
               e.created_at,
               COUNT(f.id) as failed_count,
               MAX(f.payload->>'error') as last_error,
               MAX(f.created_at) as last_failed_at
        FROM events e
        JOIN events f ON f.event_type = 'webhook.failed'
                     AND f.payload->>'dispatch_event_id' = e.id::text
                     AND f.organization_id = :org
        WHERE e.event_type = 'webhook.dispatch'
          AND e.id = :id
          AND e.organization_id = :org
        GROUP BY e.id, e.payload, e.created_at
    """)
    row = db.execute(query, {"org": str(ctx.organization_id), "id": str(id)}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook DLQ entry not found")
    return dict(row)


@router.post("/dlq/webhooks/{id}/retry")
def retry_dlq_webhook(
    id: uuid.UUID,
    db: Session = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
):
    query = text("""
        DELETE FROM events 
        WHERE event_type='webhook.failed' 
          AND payload->>'dispatch_event_id' = :id 
          AND organization_id = :org
    """)
    res = db.execute(query, {"org": str(ctx.organization_id), "id": str(id)})
    db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="Webhook not found or not dead-lettered")
    return {"ok": True, "deleted_failures": res.rowcount}


@router.post("/dlq/webhooks/{id}/resolve")
def resolve_dlq_webhook(
    id: uuid.UUID,
    db: Session = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
):
    payload = {"dispatch_event_id": str(id), "status_code": 0, "source": "manual_resolve"}
    query = text("""
        INSERT INTO events (id, organization_id, event_type, payload, severity, entity_type, created_at) 
        VALUES (gen_random_uuid(), :org, 'webhook.delivered', cast(:payload as jsonb), 'info', 'webhook_subscription', now())
    """)
    db.execute(query, {"org": str(ctx.organization_id), "payload": json.dumps(payload)})
    db.commit()
    return {"ok": True}


@router.delete("/dlq/webhooks/{id}")
def purge_dlq_webhook(
    id: uuid.UUID,
    db: Session = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
):
    query = text("""
        DELETE FROM events 
        WHERE event_type='webhook.failed' 
          AND payload->>'dispatch_event_id' = :id 
          AND organization_id = :org
    """)
    res = db.execute(query, {"org": str(ctx.organization_id), "id": str(id)})
    db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="Webhook not found or not dead-lettered")
    return {"ok": True, "deleted_failures": res.rowcount}


# ── Task dead-letter governance endpoints (INV-DLQ-2, INV-DLQ-3) ──────────────

from typing import Any  # noqa: E402

from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, model_validator  # noqa: E402

from xiosync.services.workflows import (  # noqa: E402
    DeadLetterNotFoundError,
    WorkflowService,
)


def _dlq_problem(code: str, detail: str, status: int) -> dict:
    return {"code": code, "detail": detail, "status": status}


@router.get("/dlq/{dead_letter_id}")
def get_dead_letter(
    dead_letter_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    """INV-DLQ-1: retrieve a task dead-letter record by ID."""
    ctx = request.state.org_context
    svc = WorkflowService(db)
    record = svc.get_dead_letter(ctx, dead_letter_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content=_dlq_problem(
                "dead_letter_not_found", f"Dead letter {dead_letter_id} not found", 404
            ),
            media_type="application/problem+json",
        )
    return {
        "id": str(record.id),
        "task_id": str(record.task_id),
        "state": record.state,
        "failure_reason": record.failure_reason,
        "proposal_id": str(record.proposal_id) if record.proposal_id else None,
        "attempts": record.attempts,
        "diagnosis": record.diagnosis,
        "stack_trace": record.stack_trace,
    }


class ProposeRequest(BaseModel):
    diagnosis: dict[str, Any]


@router.post("/dlq/{dead_letter_id}/propose")
def propose_dlq_correction(
    dead_letter_id: uuid.UUID,
    body: ProposeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """INV-DLQ-2: attach a diagnosis proposal to an open dead-letter record."""
    ctx = request.state.org_context
    svc = WorkflowService(db)
    try:
        proposal_id = svc.propose_dlq_correction(ctx, dead_letter_id, diagnosis=body.diagnosis)
    except DeadLetterNotFoundError:
        return JSONResponse(
            status_code=404,
            content=_dlq_problem(
                "dead_letter_not_found", f"Dead letter {dead_letter_id} not found", 404
            ),
            media_type="application/problem+json",
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=409,
            content=_dlq_problem("proposal_not_accepted", str(exc), 409),
        )
    return {"proposal_id": str(proposal_id), "state": "investigating"}


class ResolveRequest(BaseModel):
    explicit_approval: bool

    @model_validator(mode="after")
    def require_explicit_approval(self) -> "ResolveRequest":
        if not self.explicit_approval:
            raise ValueError("explicit_approval must be true to resolve a dead letter")
        return self


@router.post("/dlq/{dead_letter_id}/resolve")
def resolve_dead_letter(
    dead_letter_id: uuid.UUID,
    body: ResolveRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """INV-DLQ-3: resolve a dead-letter record that has been investigated."""
    ctx = request.state.org_context
    svc = WorkflowService(db)
    try:
        svc.resolve_dead_letter(ctx, dead_letter_id, explicit_approval=body.explicit_approval)
    except DeadLetterNotFoundError:
        return JSONResponse(
            status_code=404,
            content=_dlq_problem(
                "dead_letter_not_found", f"Dead letter {dead_letter_id} not found", 404
            ),
            media_type="application/problem+json",
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=409,
            content=_dlq_problem("resolve_not_permitted", str(exc), 409),
        )
    return {"state": "resolved"}


register_router(
    router, prefix="/api/v1", tags=["DLQ"], dependencies=[require_capability("dlq.manage")]
)
