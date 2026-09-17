import json
import uuid
import logging
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
    rows = db.execute(query, {
        "org": str(ctx.organization_id),
        "max_attempts": max_attempts,
        "limit": limit,
        "offset": offset,
    }).mappings().all()
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
    payload = {
        "dispatch_event_id": str(id),
        "status_code": 0,
        "source": "manual_resolve"
    }
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

register_router(router, prefix='/api/v1', tags=['DLQ'], dependencies=[require_capability('dlq.manage')])
