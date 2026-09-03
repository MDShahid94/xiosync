"""Cross-org sharing API (Phase 2 — Gap G-4)."""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["shares"])


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateShareRequest(_S):
    resource_type: str
    resource_id: uuid.UUID
    target_org_id: uuid.UUID | None = None
    permissions: list[str] | None = Field(default=None)


@router.post("/shares", status_code=201, summary="Create a resource share", response_model=None)
def create_share(payload: CreateShareRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.services.sharing import SharingService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = SharingService(session, enabled=True)
    try:
        rec = svc.create_share(
            ctx,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            target_org_id=payload.target_org_id,
            permissions=payload.permissions,
        )
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/share_error",
                "title": "Share creation failed",
                "status": 422,
                "detail": str(exc),
            },
        )
    return {"id": str(rec.id), "resource_type": rec.resource_type, "state": rec.state}


@router.get("/shares", summary="List outgoing shares")
def list_shares(request: Request, resource_type: str | None = None) -> list[dict[str, Any]]:
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.services.sharing import SharingService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = SharingService(session, enabled=True)
    recs = svc.list_shares(ctx, resource_type=resource_type)
    return [
        {
            "id": str(r.id),
            "resource_type": r.resource_type,
            "resource_id": str(r.resource_id),
            "state": r.state,
        }
        for r in recs
    ]


@router.post("/shares/{share_id}/revoke", summary="Revoke a share", response_model=None)
def revoke_share(share_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.services.sharing import SharingService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = SharingService(session, enabled=True)
    try:
        svc.revoke_share(ctx, share_id)
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/share_error",
                "title": "Revoke failed",
                "status": 422,
                "detail": str(exc),
            },
        )
    return {"share_id": str(share_id), "state": "revoked"}

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["shares"],
    dependencies=[require_capability("share.manage")],
)
