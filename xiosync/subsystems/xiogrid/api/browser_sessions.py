"""Browser Sessions CRUD API endpoints."""
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["browser-sessions"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateBrowserSessionRequest(_S):
    pool_id: uuid.UUID

@router.post("/browser-sessions", status_code=201, summary="Create a browser session", response_model=None)
def create_session(payload: CreateBrowserSessionRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.services.browser_sessions import BrowserSessionService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserSessionService(session)

    try:
        sess = svc.create_session(ctx, pool_id=payload.pool_id)
        return {"id": str(sess.id), "pool_id": str(sess.pool_id)}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/session_creation_failed",
                "title": "Browser session creation failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/browser-sessions", summary="List browser sessions", response_model=None)
def list_sessions(request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.services.browser_sessions import BrowserSessionService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserSessionService(session)

    sessions = svc.list_sessions(ctx)
    return {"sessions": [{"id": str(s.id), "pool_id": str(s.pool_id)} for s in sessions]}

@router.get("/browser-sessions/{session_id}/health", summary="Get browser session health", response_model=None)
def get_session_health(session_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.services.browser_sessions import BrowserSessionService, BrowserSessionNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserSessionService(session)

    try:
        health = svc.verify_session(ctx, session_id)
        # SessionHealthRecord probably has some fields we can return
        return {"id": str(session_id), "status": health.status if hasattr(health, 'status') else "ok"}
    except BrowserSessionNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser session not found",
                "status": 404,
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/health_check_failed",
                "title": "Health check failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.delete("/browser-sessions/{session_id}", summary="Terminate browser session", response_model=None)
def terminate_session(session_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.services.browser_sessions import BrowserSessionService, BrowserSessionNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserSessionService(session)

    try:
        svc.terminate_session(ctx, session_id)
        return {"id": str(session_id), "terminated": True}
    except BrowserSessionNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser session not found",
                "status": 404,
            },
        )

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["browser-sessions"],
    dependencies=[require_capability("browser_session.manage")],
)
