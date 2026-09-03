"""Secret reference CRUD API (Phase 2 — Gap G-4)."""
from __future__ import annotations
import uuid
from typing import Any, cast
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["secrets"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateSecretRequest(_S):
    name: str
    provider: str
    ref_config: dict[str, Any] = Field(description="Provider-specific config")

class RotateSecretRequest(_S):
    new_ref_config: dict[str, Any]

@router.post("/secrets", status_code=201, summary="Create a secret reference", response_model=None)
def create_secret(payload: CreateSecretRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.secrets import SecretRefService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = SecretRefService(session)
    try:
        rec = svc.create_secret(ctx, name=payload.name, provider=payload.provider,
                                ref_config=payload.ref_config, created_by=ctx.actor_id)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/secret_error", "title": "Secret creation failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "name": rec.name, "provider": rec.provider, "state": rec.state}

@router.get("/secrets", summary="List secret references")
def list_secrets(request: Request) -> list[dict[str, Any]]:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.secrets import SecretRefService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = SecretRefService(session)
    recs = svc.list_secrets(ctx)
    return [{"id": str(r.id), "name": r.name, "provider": r.provider, "state": r.state} for r in recs]

@router.post("/secrets/{secret_id}/rotate", summary="Rotate a secret", response_model=None)
def rotate_secret(secret_id: uuid.UUID, payload: RotateSecretRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.secrets import SecretRefService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = SecretRefService(session)
    try:
        rec = svc.rotate_secret(ctx, secret_id, new_ref_config=payload.new_ref_config)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/secret_error", "title": "Rotation failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "name": rec.name, "state": rec.state}

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["secrets"],
    dependencies=[require_capability("secret.manage")],
)
