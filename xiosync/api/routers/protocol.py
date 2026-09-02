"""Protocol evolution and document management API (Phase 3 — Gaps G-5, G-7)."""
from __future__ import annotations
import uuid
from typing import Any, cast
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["protocol"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

# --- Protocol evolution endpoints ---

class RecordEvolutionRequest(_S):
    evolution_type: str = Field(description="schema_migration, capability_added, config_change, protocol_upgrade")
    description: str
    rationale: str | None = None
    diff: dict[str, Any] | None = None
    version: str | None = None

@router.post("/protocol/evolve", status_code=201, summary="Record a protocol evolution", response_model=None)
def record_evolution(payload: RecordEvolutionRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.protocol import ProtocolService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ProtocolService(session)
    try:
        rec = svc.record_evolution(ctx, evolution_type=payload.evolution_type, description=payload.description,
                                   rationale=payload.rationale, diff=payload.diff, version=payload.version)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/protocol_error", "title": "Evolution recording failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "evolution_type": rec.evolution_type, "description": rec.description,
            "operation_id": str(rec.operation_id), "event_id": str(rec.event_id)}

@router.get("/protocol/history", summary="Get protocol evolution history")
def get_history(request: Request, evolution_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.protocol import ProtocolService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ProtocolService(session)
    return svc.get_evolution_history(ctx, evolution_type=evolution_type, limit=limit)

@router.get("/protocol/version", summary="Get current protocol version")
def get_version(request: Request) -> dict[str, Any]:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.protocol import ProtocolService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ProtocolService(session)
    version = svc.get_current_version(ctx)
    return {"version": version, "protocol": "xiosync"}

# --- Document collection endpoints ---

class CreateCollectionRequest(_S):
    name: str
    slug: str
    doc_type: str = "custom"
    description: str | None = None
    version: str = "1.0.0"

class AddPageRequest(_S):
    title: str
    provider_type: str = "inline"
    uri: str
    content_type: str | None = None
    page_order: int | None = None
    parent_page_id: uuid.UUID | None = None

class PublishCollectionRequest(_S):
    new_version: str | None = None

@router.post("/documents", status_code=201, summary="Create a document collection", response_model=None)
def create_collection(payload: CreateCollectionRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.documents import DocumentService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = DocumentService(session)
    try:
        rec = svc.create_collection(ctx, name=payload.name, slug=payload.slug, doc_type=payload.doc_type,
                                    description=payload.description, version=payload.version)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/document_error", "title": "Collection creation failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "name": rec.name, "slug": rec.slug, "state": rec.state,
            "doc_type": rec.doc_type, "version": rec.version}

@router.get("/documents", summary="List document collections")
def list_collections(request: Request, doc_type: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.documents import DocumentService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = DocumentService(session)
    recs = svc.list_collections(ctx, doc_type=doc_type, state=state)
    return [{"id": str(r.id), "name": r.name, "slug": r.slug, "state": r.state,
             "doc_type": r.doc_type, "version": r.version, "page_count": r.page_count} for r in recs]

@router.post("/documents/{collection_id}/pages", status_code=201, summary="Add a page to a collection", response_model=None)
def add_page(collection_id: uuid.UUID, payload: AddPageRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.documents import DocumentService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = DocumentService(session)
    try:
        page = svc.add_page(ctx, collection_id=collection_id, title=payload.title,
                            provider_type=payload.provider_type, uri=payload.uri,
                            content_type=payload.content_type, page_order=payload.page_order,
                            parent_page_id=payload.parent_page_id)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/document_error", "title": "Page addition failed",
            "status": 422, "detail": str(exc),
        })
    return {"artifact_id": str(page.artifact_id), "title": page.title, "page_order": page.page_order}

@router.get("/documents/{collection_id}/pages", summary="Get pages in a collection")
def get_pages(collection_id: uuid.UUID, request: Request) -> list[dict[str, Any]]:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.documents import DocumentService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = DocumentService(session)
    pages = svc.get_collection_pages(ctx, collection_id)
    return [{"artifact_id": str(p.artifact_id), "title": p.title, "page_order": p.page_order,
             "parent_page_id": str(p.parent_page_id) if p.parent_page_id else None,
             "content_type": p.content_type, "uri": p.uri} for p in pages]

@router.post("/documents/{collection_id}/publish", summary="Publish a collection", response_model=None)
def publish_collection(collection_id: uuid.UUID, payload: PublishCollectionRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.documents import DocumentService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = DocumentService(session)
    try:
        rec = svc.publish_collection(ctx, collection_id, new_version=payload.new_version)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/document_error", "title": "Publish failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(rec.id), "name": rec.name, "state": rec.state, "version": rec.version,
            "page_count": rec.page_count}
