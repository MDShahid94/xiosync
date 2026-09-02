"""Worker management API (Phase 2 — Gap G-4)."""
from __future__ import annotations
import uuid
from typing import Any, cast
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["workers"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class RegisterWorkerRequest(_S):
    enrollment_token: str
    public_key: str
    pool_type: str = "volunteer"
    software_version: str | None = None
    capability_manifest: list[Any] | None = None

class ApproveWorkerRequest(_S):
    approved_by: uuid.UUID

@router.post("/workers/register", status_code=201, summary="Register a worker", response_model=None)
def register_worker(payload: RegisterWorkerRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.workers import WorkerService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WorkerService(session)
    try:
        rec = svc.register_worker(ctx, enrollment_token=payload.enrollment_token,
                                  public_key=payload.public_key, pool_type=payload.pool_type,
                                  software_version=payload.software_version,
                                  capability_manifest=payload.capability_manifest)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/worker_error", "title": "Registration failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "enrollment_state": rec.enrollment_state}

@router.post("/workers/{enrollment_id}/approve", summary="Approve a worker", response_model=None)
def approve_worker(enrollment_id: uuid.UUID, payload: ApproveWorkerRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.workers import WorkerService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WorkerService(session)
    try:
        rec = svc.approve_worker(ctx, enrollment_id, approved_by=payload.approved_by)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/worker_error", "title": "Approval failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "enrollment_state": rec.enrollment_state}
