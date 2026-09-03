"""Compute Runtimes CRUD API endpoints."""
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["compute-runtimes"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class RegisterProviderRequest(_S):
    name: str
    provider: str
    config: dict[str, Any] = {}

class ProvisionNodeRequest(_S):
    instance_type: str
    node_metadata: dict[str, Any] = {}

@router.post("/compute-runtimes", status_code=201, summary="Register provider", response_model=None)
def register_provider(payload: RegisterProviderRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.compute_runtimes import ComputeRuntimeService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ComputeRuntimeService(session)

    try:
        provider = svc.register_provider(ctx, name=payload.name, provider=payload.provider, config=payload.config)
        return {"id": str(provider.id), "name": provider.name}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/provider_registration_failed",
                "title": "Provider registration failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/compute-runtimes", summary="List providers", response_model=None)
def list_providers(
    request: Request,
    project_id: uuid.UUID | None = Query(default=None, description="Filter providers by project"),
) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.compute_runtimes import ComputeRuntimeService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ComputeRuntimeService(session)

    providers = svc.list_providers(ctx, project_id=project_id)
    return {"providers": [{"id": str(p.id), "name": p.name} for p in providers]}

@router.post("/compute-runtimes/{runtime_id}/nodes", status_code=201, summary="Provision node", response_model=None)
def provision_node(runtime_id: uuid.UUID, payload: ProvisionNodeRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.compute_runtimes import ComputeRuntimeService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ComputeRuntimeService(session)

    try:
        node = svc.provision_node(ctx, runtime_id=runtime_id, spec=payload.node_metadata)
        return {"id": str(node.id), "runtime_id": str(node.runtime_id)}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/provision_failed",
                "title": "Provision failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/compute-runtimes/{runtime_id}/nodes", summary="List nodes", response_model=None)
def list_nodes(runtime_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.compute_runtimes import ComputeRuntimeService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ComputeRuntimeService(session)

    nodes = svc.list_nodes(ctx, runtime_id=runtime_id)
    return {"nodes": [{"id": str(n.id), "runtime_id": str(n.runtime_id)} for n in nodes]}

@router.delete("/compute-runtimes/nodes/{node_id}", summary="Terminate node", response_model=None)
def terminate_node(node_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.compute_runtimes import ComputeRuntimeService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ComputeRuntimeService(session)

    try:
        svc.terminate_node(ctx, node_id)
        return {"id": str(node_id), "terminated": True}
    except Exception as exc:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Node not found or terminate failed",
                "status": 404,
                "detail": str(exc),
            },
        )

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["compute-runtimes"],
    dependencies=[require_capability("compute_runtime.manage")],
)
