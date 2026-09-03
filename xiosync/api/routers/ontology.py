"""Ontology API — edges and memory CRUD (Phase 2 — Gap G-4)."""
from __future__ import annotations
import uuid
from typing import Any, cast
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["ontology"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateEdgeRequest(_S):
    source_id: uuid.UUID
    target_id: uuid.UUID
    edge_type: str
    graph_class: str
    weight: float | None = None

class CreateMemoryRequest(_S):
    owner_actor_id: uuid.UUID
    kind: str
    content: dict[str, Any]
    visibility: str = "private"

@router.post("/edges", status_code=201, summary="Create an ontology edge", response_model=None)
def create_edge(payload: CreateEdgeRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.persistence.ontology import EdgeRepository
    from xiosync.services.ontology import EdgeService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = EdgeService(EdgeRepository(session))
    try:
        edge_id = svc.create_edge(ctx, source_id=payload.source_id, target_id=payload.target_id,
                                  edge_type=payload.edge_type, graph_class=payload.graph_class,
                                  weight=payload.weight)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/edge_error", "title": "Edge creation failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(edge_id)}

@router.post("/memories", status_code=201, summary="Create a memory entry", response_model=None)
def create_memory(payload: CreateMemoryRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.persistence.ontology import MemoryRepository
    from xiosync.services.ontology import MemoryService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = MemoryService(MemoryRepository(session))
    try:
        memory_id = svc.create_memory(ctx, owner_actor_id=payload.owner_actor_id, kind=payload.kind,
                                      content=payload.content, visibility=payload.visibility)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/memory_error", "title": "Memory creation failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(memory_id)}

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["ontology"],
    dependencies=[require_capability("ontology.manage")],
)
