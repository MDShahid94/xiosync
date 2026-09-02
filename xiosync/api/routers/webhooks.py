"""Webhook subscription API (Phase 2 — Gap G-4)."""
from __future__ import annotations
import uuid
from typing import Any, cast
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["webhooks"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateWebhookRequest(_S):
    url: str
    event_types: list[str]
    headers: dict[str, Any] | None = None

@router.post("/webhooks", status_code=201, summary="Create a webhook subscription", response_model=None)
def create_webhook(payload: CreateWebhookRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.webhooks import WebhookService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WebhookService(session)
    try:
        rec = svc.create_subscription(ctx, url=payload.url, event_types=payload.event_types,
                                      headers=payload.headers)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/webhook_error", "title": "Webhook creation failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "url": rec.url, "state": rec.state}

@router.get("/webhooks", summary="List webhook subscriptions")
def list_webhooks(request: Request) -> list[dict[str, Any]]:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.webhooks import WebhookService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WebhookService(session)
    recs = svc.list_subscriptions(ctx)
    return [{"id": str(r.id), "url": r.url, "event_types": r.event_types, "state": r.state} for r in recs]

@router.post("/webhooks/{subscription_id}/pause", summary="Pause a webhook", response_model=None)
def pause_webhook(subscription_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.webhooks import WebhookService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WebhookService(session)
    try:
        svc.pause_subscription(ctx, subscription_id)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/webhook_error", "title": "Pause failed",
            "status": 422, "detail": str(exc),
        })
    return {"subscription_id": str(subscription_id), "state": "paused"}
