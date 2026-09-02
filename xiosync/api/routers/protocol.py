"""Protocol evolution and document management API (Phase 3 — Gaps G-5, G-7)."""
from __future__ import annotations
import uuid
from typing import Any, cast
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
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
    slug: str
    content_format: str = "markdown"
    inline_content: str | None = None
    artifact_id: uuid.UUID | None = None
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
                            slug=payload.slug, content_format=payload.content_format,
                            inline_content=payload.inline_content, artifact_id=payload.artifact_id,
                            page_order=payload.page_order, parent_page_id=payload.parent_page_id)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/document_error", "title": "Page addition failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(page.id), "title": page.title, "slug": page.slug, "page_order": page.page_order}

@router.get("/documents/{collection_id}/pages", summary="Get pages in a collection")
def get_pages(collection_id: uuid.UUID, request: Request) -> list[dict[str, Any]]:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.documents import DocumentService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = DocumentService(session)
    pages = svc.get_collection_pages(ctx, collection_id)
    return [{"id": str(p.id), "title": p.title, "slug": p.slug, "page_order": p.page_order,
             "parent_page_id": str(p.parent_page_id) if p.parent_page_id else None,
             "content_format": p.content_format, "depth": p.depth} for p in pages]

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

# --- AI-agent-readable documentation endpoints ---

@router.get("/docs/{collection_slug}/llms.txt", summary="AI-readable index (llms.txt)", response_class=PlainTextResponse)
def llms_txt(collection_slug: str, request: Request) -> Any:
    from sqlalchemy import select as sa_select
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.persistence.models.documents import DocumentCollection
    from xiosync.services.documents import DocumentService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    coll = session.scalar(
        sa_select(DocumentCollection).where(
            DocumentCollection.organization_id == ctx.organization_id,
            DocumentCollection.slug == collection_slug,
        ).order_by(DocumentCollection.created_at.desc()).limit(1)
    )
    if coll is None:
        return PlainTextResponse("# Not Found\n", status_code=404)
    svc = DocumentService(session)
    return PlainTextResponse(svc.generate_llms_txt(ctx, coll.id), media_type="text/plain; charset=utf-8")

@router.get("/docs/{collection_slug}/llms-full.txt", summary="AI-readable full dump (llms-full.txt)", response_class=PlainTextResponse)
def llms_full_txt(collection_slug: str, request: Request) -> Any:
    from sqlalchemy import select as sa_select
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.persistence.models.documents import DocumentCollection
    from xiosync.services.documents import DocumentService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    coll = session.scalar(
        sa_select(DocumentCollection).where(
            DocumentCollection.organization_id == ctx.organization_id,
            DocumentCollection.slug == collection_slug,
        ).order_by(DocumentCollection.created_at.desc()).limit(1)
    )
    if coll is None:
        return PlainTextResponse("# Not Found\n", status_code=404)
    svc = DocumentService(session)
    return PlainTextResponse(svc.generate_llms_full_txt(ctx, coll.id), media_type="text/plain; charset=utf-8")

@router.get("/docs/{collection_slug}/pages/{page_slug}.md", summary="Per-page raw Markdown", response_class=PlainTextResponse)
def page_markdown(collection_slug: str, page_slug: str, request: Request) -> Any:
    from sqlalchemy import select as sa_select
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.persistence.models.documents import DocumentCollection, DocumentPage
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    coll = session.scalar(
        sa_select(DocumentCollection).where(
            DocumentCollection.organization_id == ctx.organization_id,
            DocumentCollection.slug == collection_slug,
        ).order_by(DocumentCollection.created_at.desc()).limit(1)
    )
    if coll is None:
        return PlainTextResponse("# Not Found\n", status_code=404)
    page = session.scalar(
        sa_select(DocumentPage).where(
            DocumentPage.collection_id == coll.id,
            DocumentPage.slug == page_slug,
        )
    )
    if page is None:
        return PlainTextResponse("# Page Not Found\n", status_code=404)
    if page.inline_content:
        return PlainTextResponse(page.inline_content, media_type="text/markdown; charset=utf-8")
    return PlainTextResponse(f"# {page.title}\n\n*Content stored at artifact {page.artifact_id}*\n",
                             media_type="text/markdown; charset=utf-8")
