"""Mesh Networks CRUD API endpoints."""
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["mesh-networks"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateNetworkRequest(_S):
    name: str
    network_type: str
    config: dict[str, Any] = {}

class AddNodeRequest(_S):
    node_id: uuid.UUID
    node_address: str

@router.post("/mesh-networks", status_code=201, summary="Create network", response_model=None)
def create_network(payload: CreateNetworkRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.mesh_networks import MeshNetworkService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = MeshNetworkService(session)

    try:
        net = svc.create_network(ctx, name=payload.name, network_type=payload.network_type, config=payload.config)
        return {"id": str(net.id), "name": net.name}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/network_creation_failed",
                "title": "Network creation failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/mesh-networks", summary="List networks", response_model=None)
def list_networks(
    request: Request,
    project_id: uuid.UUID | None = Query(default=None, description="Filter networks by project"),
) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.mesh_networks import MeshNetworkService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = MeshNetworkService(session)

    networks = svc.list_networks(ctx, project_id=project_id)
    return {"networks": [{"id": str(n.id), "name": n.name} for n in networks]}

@router.post("/mesh-networks/{network_id}/nodes", status_code=201, summary="Add node", response_model=None)
def add_node(network_id: uuid.UUID, payload: AddNodeRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.mesh_networks import MeshNetworkService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = MeshNetworkService(session)

    try:
        svc.add_node(ctx, network_id=network_id, node_id=payload.node_id, address=payload.node_address)
        return {"network_id": str(network_id), "node_id": str(payload.node_id)}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/add_node_failed",
                "title": "Add node failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.delete("/mesh-networks/{network_id}/nodes/{node_id}", summary="Remove node", response_model=None)
def remove_node(network_id: uuid.UUID, node_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.mesh_networks import MeshNetworkService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = MeshNetworkService(session)

    try:
        svc.remove_node(ctx, network_id=network_id, node_id=node_id)
        return {"id": str(node_id), "removed": True}
    except Exception as exc:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Node not found or remove failed",
                "status": 404,
                "detail": str(exc),
            },
        )

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["mesh-networks"],
    dependencies=[require_capability("mesh_network.manage")],
)
