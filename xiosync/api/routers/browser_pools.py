"""Browser Pools CRUD API endpoints."""
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["browser-pools"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateBrowserPoolRequest(_S):
    name: str
    engine_type: str = "chromium"
    max_instances: int = 5

class ScaleBrowserPoolRequest(_S):
    replicas: int

@router.post("/browser-pools", status_code=201, summary="Create a browser pool", response_model=None)
def create_pool(payload: CreateBrowserPoolRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    try:
        pool = svc.create_pool(ctx, name=payload.name, engine_type=payload.engine_type, max_instances=payload.max_instances)
        return {"id": str(pool.id), "name": pool.name}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/pool_creation_failed",
                "title": "Browser pool creation failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/browser-pools", summary="List browser pools", response_model=None)
def list_pools(
    request: Request,
    project_id: uuid.UUID | None = Query(default=None, description="Filter pools by project"),
) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    pools = svc.list_pools(ctx, project_id=project_id)
    return {"pools": [{"id": str(p.id), "name": p.name} for p in pools]}

@router.get("/browser-pools/{pool_id}", summary="Get browser pool", response_model=None)
def get_pool(pool_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService, BrowserPoolNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    try:
        pool = svc.get_pool(ctx, pool_id)
        return {"id": str(pool.id), "name": pool.name}
    except BrowserPoolNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser pool not found",
                "status": 404,
            },
        )

@router.post("/browser-pools/{pool_id}/scale", summary="Scale browser pool", response_model=None)
def scale_pool(pool_id: uuid.UUID, payload: ScaleBrowserPoolRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService, BrowserPoolNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    try:
        svc.scale_pool(ctx, pool_id, target_instances=payload.replicas)
        return {"id": str(pool_id), "scaled": True}
    except BrowserPoolNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser pool not found",
                "status": 404,
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/scale_failed",
                "title": "Scale failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.delete("/browser-pools/{pool_id}", summary="Destroy browser pool", response_model=None)
def destroy_pool(pool_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService, BrowserPoolNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    try:
        svc.destroy_pool(ctx, pool_id)
        return {"id": str(pool_id), "destroyed": True}
    except BrowserPoolNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser pool not found",
                "status": 404,
            },
        )

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["browser-pools"],
    dependencies=[require_capability("browser_pool.manage")],
)
