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
    capability_manifest: list[str] | None = None

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

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["workers"],
    dependencies=[require_capability("worker.manage")],
)

# Public worker routes — authenticated by per-request secrets, not Bearer token.
public_router = APIRouter(tags=["workers"])
register_router(public_router, prefix='/api/v1', tags=["workers"])


# ── Self-enroll (autonomous Colab / VM workers) ─────────────────────────────

class SelfEnrollRequest(_S):
    """A worker runtime enrolling itself using the shared org secret."""
    worker_org_secret: str
    runtime_type: str = "colab"           # colab | vm | mac | docker
    tailscale_ip: str | None = None
    reported_caps: list[str] = []
    software_version: str | None = None
    public_key: str = ""                  # optional — can be empty for ephemeral workers


class HeartbeatRequest(_S):
    tailscale_ip: str | None = None
    reported_caps: list[str] | None = None


@public_router.post(
    "/workers/self-enroll",
    status_code=201,
    summary="Autonomous worker self-enrollment (Colab / VM runtimes)",
    response_model=None,
)
def self_enroll(payload: SelfEnrollRequest, request: Request) -> dict[str, Any] | JSONResponse:
    """Enroll a worker using the shared XIOSYNC_WORKER_ORG_SECRET.

    Successful enroll returns enrollment_id + auto-generated enrollment_token.
    The worker should store these and use them to call /workers/self-enroll/credential
    once an operator approves, OR the enrollment is auto-approved here if the
    secret is valid (self-approval mode for trusted runtimes).
    """
    import os
    from datetime import UTC, datetime
    from sqlalchemy import text
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.workers import WorkerService
    from xiosync.platform.ids import new_id

    expected_secret = os.environ.get("XIOSYNC_WORKER_ORG_SECRET", "")
    if not expected_secret or payload.worker_org_secret != expected_secret:
        return JSONResponse(status_code=401, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/unauthorized",
            "title": "Invalid worker org secret",
            "status": 401,
        })

    # Build a synthetic OrgContext for Org Zero — self-enroll is authenticated
    # by worker_org_secret in the body, not a Bearer token, so request.state
    # has no org_context. We construct one directly.
    import uuid as _uuid  # noqa: PLC0415
    from xiosync.domain.context import OrgContext, PlatformRole, MembershipRole  # noqa: PLC0415
    from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415
    _ORG_ZERO = _uuid.UUID("00000000-0000-7000-8000-000000000000")
    ctx = OrgContext(
        auth_identity_id=_ORG_ZERO, actor_id=_ORG_ZERO,
        organization_id=_ORG_ZERO, session_id=_ORG_ZERO,
        platform_role=PlatformRole.PLATFORM_ADMIN,
        membership_role=MembershipRole.ORG_OWNER,
    )
    session = cast(OrmSession, OrmSession(get_engine()))
    svc = WorkerService(session)

    # Generate a one-time enrollment token derived from org secret + timestamp
    import hashlib, secrets
    enrollment_token = secrets.token_urlsafe(32)

    # Create a system actor for this worker so the FK constraint is satisfied
    actor_id = new_id()
    try:
        session.execute(
            text("""
                INSERT INTO actors
                  (id, organization_id, actor_type, actor_subtype, alias, state,
                   lifecycle_phase, trust_tier, health_status, created_at)
                VALUES
                  (:id, :org_id, 'worker', :subtype, :alias, 'active',
                   'operational', 'newcomer', 'healthy', now())
                ON CONFLICT DO NOTHING
            """),
            {
                "id": str(actor_id),
                "org_id": str(ctx.organization_id),
                "subtype": payload.runtime_type,
                "alias": f"{payload.runtime_type}-{str(actor_id)[:8]}",
            },
        )
        session.flush()
    except Exception as _actor_err:
        import logging
        logging.getLogger(__name__).warning("actor creation failed: %s", _actor_err)

    try:
        rec = svc.register_worker(
            ctx,
            enrollment_token=enrollment_token,
            public_key=payload.public_key or f"ephemeral-{new_id()}",
            pool_type=payload.runtime_type,
            worker_id=actor_id,
            software_version=payload.software_version,
            capability_manifest=payload.reported_caps or [],
        )
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/worker_error",
            "title": "Self-enroll failed",
            "status": 422, "detail": str(exc),
        })

    # Auto-approve (trusted runtime — secret validated above)
    try:
        rec = svc.approve_worker(ctx, rec.id, approved_by=ctx.actor_id)
    except Exception:
        pass  # non-fatal — operator can manually approve later

    # Store tailscale_ip, reported_caps, runtime_type
    if payload.tailscale_ip or payload.reported_caps or payload.runtime_type:
        session.execute(
            text("""
                UPDATE worker_enrollments
                SET tailscale_ip = :ts_ip,
                    reported_caps = cast(:caps as jsonb),
                    runtime_type = :rtype,
                    last_seen_at = now()
                WHERE id = :eid
            """),
            {
                "ts_ip": payload.tailscale_ip,
                "caps": __import__("json").dumps(payload.reported_caps or []),
                "rtype": payload.runtime_type,
                "eid": str(rec.id),
            },
        )
        session.commit()

    return {
        "enrollment_id": str(rec.id),
        "enrollment_state": rec.enrollment_state,
        "enrollment_token": enrollment_token,
        "note": "auto-approved — use enrollment_token for credential issuance",
    }


@router.post(
    "/workers/{enrollment_id}/heartbeat",
    summary="Worker heartbeat — update last_seen_at and reported capabilities",
    response_model=None,
)
def worker_heartbeat(
    enrollment_id: uuid.UUID,
    payload: HeartbeatRequest,
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """Called by worker runtimes every N seconds to report liveness."""
    import json
    from sqlalchemy import text
    from sqlalchemy.orm import Session as OrmSession

    session = cast(OrmSession, request.state.org_session)

    updates = {"last_seen_at": "now()", "eid": str(enrollment_id)}
    set_parts = ["last_seen_at = now()"]

    if payload.tailscale_ip is not None:
        set_parts.append("tailscale_ip = :ts_ip")
        updates["ts_ip"] = payload.tailscale_ip
    if payload.reported_caps is not None:
        set_parts.append("reported_caps = cast(:caps as jsonb)")
        updates["caps"] = json.dumps(payload.reported_caps)

    result = session.execute(
        text(f"UPDATE worker_enrollments SET {', '.join(set_parts)} WHERE id = :eid RETURNING id"),
        updates,
    ).fetchone()

    if not result:
        return JSONResponse(status_code=404, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/not_found",
            "title": "Worker enrollment not found",
            "status": 404,
        })

    session.commit()
    return {"enrollment_id": str(enrollment_id), "last_seen_at": "now", "status": "ok"}
