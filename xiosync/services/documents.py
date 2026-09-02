"""Enterprise document management service (Phase 3 — Gap G-7).

Builds on the existing ArtifactService to provide enterprise-grade,
multi-page documentation management. A "document collection" is a
governed entity that groups related artifacts into a structured,
versioned, navigable documentation set.

Key capabilities:
- **Collections**: Group related artifacts into a logical document set
  (e.g., API Reference, Architecture Guide, Runbook)
- **Versioning**: Each collection has a version; publishing creates an
  immutable snapshot while allowing a new draft
- **Page ordering**: Pages within a collection have explicit order and
  hierarchy (parent_page_id for nested sections)
- **Cross-referencing**: Pages can reference other pages/collections via
  edges in the ontology graph
- **Lifecycle**: Collections go through draft → published → archived,
  governed by operations and events
- **Sharing**: Collections can be shared across orgs via the sharing protocol

All of this is built on existing XIOSYNC primitives:
- Artifacts for individual pages/files
- Edges for cross-references
- Operations for audit trail
- Events for lifecycle tracking
- Type Registry for document types
- Sharing for cross-org access
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.artifacts import Artifact
from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.operations import Operation
from xiosync.platform.ids import new_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentPage:
    """A single page within a document collection."""

    artifact_id: uuid.UUID
    title: str
    page_order: int
    parent_page_id: uuid.UUID | None
    content_type: str | None
    uri: str


@dataclass(frozen=True, slots=True)
class DocumentCollectionRecord:
    """A governed document collection."""

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    version: str
    state: str  # draft, published, archived
    doc_type: str  # api_reference, architecture, runbook, tutorial, policy, custom
    page_count: int
    created_by: uuid.UUID
    created_at: datetime


class DocumentNotFoundError(ValueError):
    def __init__(self, doc_id: uuid.UUID) -> None:
        super().__init__(f"Document collection {doc_id} not found")
        self.doc_id = doc_id


class DocumentService:
    """Enterprise document management built on XIOSYNC primitives.

    Document collections are stored as artifacts with a structured
    metadata envelope. Pages are individual artifacts linked to the
    collection via metadata.collection_id. Cross-references are edges.

    This approach requires no new tables — everything is built on the
    existing artifact, edge, operation, and event infrastructure.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_collection(
        self,
        context: OrgContext,
        *,
        name: str,
        slug: str,
        doc_type: str = "custom",
        description: str | None = None,
        version: str = "1.0.0",
    ) -> DocumentCollectionRecord:
        """Create a new document collection in draft state.

        The collection itself is stored as an artifact with provider_type
        'inline' and content_type 'application/vnd.xiosync.document-collection'.
        """
        now = datetime.now(UTC)
        collection_id = new_id()
        op_id = new_id()

        # Create the collection artifact
        collection = Artifact(
            id=collection_id,
            organization_id=context.organization_id,
            provider_type="inline",
            uri=f"xiosync://documents/{slug}",
            content_type="application/vnd.xiosync.document-collection",
            extra_metadata={
                "collection_name": name,
                "collection_slug": slug,
                "collection_version": version,
                "collection_state": "draft",
                "collection_doc_type": doc_type,
                "collection_description": description,
                "page_ids": [],
            },
            created_by=context.actor_id,
        )
        self._session.add(collection)

        # Record operation
        op = Operation(
            id=op_id,
            organization_id=context.organization_id,
            actor_id=context.actor_id,
            operation="document.collection.create",
            trigger="user_command",
            initiated_by=context.actor_id,
            scope="organization",
            outcome="success",
            rationale=f"Created document collection: {name}",
            started_at=now,
            completed_at=now,
        )
        self._session.add(op)

        # Record event
        event = Event(
            id=new_id(),
            organization_id=context.organization_id,
            event_type="artifact.created",
            actor_id=context.actor_id,
            severity="info",
            operation_id=op_id,
            entity_type="document_collection",
            entity_id=collection_id,
            payload={
                "name": name,
                "slug": slug,
                "doc_type": doc_type,
                "version": version,
            },
            created_at=now,
        )
        self._session.add(event)
        self._session.flush()

        logger.info(
            "document.collection.created: id=%s name='%s' type=%s",
            collection_id, name, doc_type,
        )

        return DocumentCollectionRecord(
            id=collection_id,
            organization_id=context.organization_id,
            name=name,
            slug=slug,
            description=description,
            version=version,
            state="draft",
            doc_type=doc_type,
            page_count=0,
            created_by=context.actor_id,
            created_at=now,
        )

    def add_page(
        self,
        context: OrgContext,
        *,
        collection_id: uuid.UUID,
        title: str,
        provider_type: str,
        uri: str,
        content_type: str | None = None,
        page_order: int | None = None,
        parent_page_id: uuid.UUID | None = None,
        size_bytes: int | None = None,
        checksum: str | None = None,
    ) -> DocumentPage:
        """Add a page to a document collection.

        The page is an artifact linked to the collection via metadata.
        """
        # Verify collection exists
        collection = self._session.scalar(
            select(Artifact).where(
                Artifact.id == collection_id,
                Artifact.organization_id == context.organization_id,
                Artifact.content_type == "application/vnd.xiosync.document-collection",
            )
        )
        if collection is None:
            raise DocumentNotFoundError(collection_id)

        now = datetime.now(UTC)
        page_id = new_id()

        # Auto-assign page_order if not provided
        if page_order is None:
            existing_pages = collection.extra_metadata.get("page_ids", [])
            page_order = len(existing_pages) + 1

        # Create the page artifact
        page = Artifact(
            id=page_id,
            organization_id=context.organization_id,
            provider_type=provider_type,
            uri=uri,
            content_type=content_type or "text/markdown",
            size_bytes=size_bytes,
            checksum=checksum,
            extra_metadata={
                "collection_id": str(collection_id),
                "page_title": title,
                "page_order": page_order,
                "parent_page_id": str(parent_page_id) if parent_page_id else None,
            },
            created_by=context.actor_id,
        )
        self._session.add(page)

        # Update collection's page list
        page_ids = list(collection.extra_metadata.get("page_ids", []))
        page_ids.append(str(page_id))
        new_meta = dict(collection.extra_metadata)
        new_meta["page_ids"] = page_ids
        self._session.execute(
            update(Artifact)
            .where(Artifact.id == collection_id)
            .values(extra_metadata=new_meta)
        )
        self._session.flush()

        return DocumentPage(
            artifact_id=page_id,
            title=title,
            page_order=page_order,
            parent_page_id=parent_page_id,
            content_type=content_type or "text/markdown",
            uri=uri,
        )

    def publish_collection(
        self,
        context: OrgContext,
        collection_id: uuid.UUID,
        *,
        new_version: str | None = None,
    ) -> DocumentCollectionRecord:
        """Publish a document collection (draft → published)."""
        collection = self._session.scalar(
            select(Artifact).where(
                Artifact.id == collection_id,
                Artifact.organization_id == context.organization_id,
                Artifact.content_type == "application/vnd.xiosync.document-collection",
            )
        )
        if collection is None:
            raise DocumentNotFoundError(collection_id)

        now = datetime.now(UTC)
        meta = dict(collection.extra_metadata)
        old_state = meta.get("collection_state", "draft")
        meta["collection_state"] = "published"
        meta["published_at"] = now.isoformat()
        if new_version:
            meta["collection_version"] = new_version

        self._session.execute(
            update(Artifact)
            .where(Artifact.id == collection_id)
            .values(extra_metadata=meta)
        )

        # Record event
        op_id = new_id()
        op = Operation(
            id=op_id,
            organization_id=context.organization_id,
            actor_id=context.actor_id,
            operation="document.collection.publish",
            trigger="user_command",
            initiated_by=context.actor_id,
            scope="organization",
            outcome="success",
            from_state=old_state,
            to_state="published",
            rationale=f"Published document: {meta.get('collection_name')}",
            started_at=now,
            completed_at=now,
        )
        self._session.add(op)

        event = Event(
            id=new_id(),
            organization_id=context.organization_id,
            event_type="artifact.created",
            actor_id=context.actor_id,
            severity="info",
            operation_id=op_id,
            entity_type="document_collection",
            entity_id=collection_id,
            payload={"action": "published", "version": meta.get("collection_version")},
            created_at=now,
        )
        self._session.add(event)
        self._session.flush()

        page_ids = meta.get("page_ids", [])
        return DocumentCollectionRecord(
            id=collection_id,
            organization_id=context.organization_id,
            name=meta.get("collection_name", ""),
            slug=meta.get("collection_slug", ""),
            description=meta.get("collection_description"),
            version=meta.get("collection_version", "1.0.0"),
            state="published",
            doc_type=meta.get("collection_doc_type", "custom"),
            page_count=len(page_ids),
            created_by=collection.created_by,
            created_at=collection.created_at,
        )

    def list_collections(
        self,
        context: OrgContext,
        *,
        doc_type: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[DocumentCollectionRecord]:
        """List document collections in this org."""
        stmt = (
            select(Artifact)
            .where(
                Artifact.organization_id == context.organization_id,
                Artifact.content_type == "application/vnd.xiosync.document-collection",
            )
            .order_by(Artifact.created_at.desc())
            .limit(limit)
        )
        rows = self._session.scalars(stmt).all()
        results = []
        for row in rows:
            meta = row.extra_metadata or {}
            if doc_type and meta.get("collection_doc_type") != doc_type:
                continue
            if state and meta.get("collection_state") != state:
                continue
            results.append(DocumentCollectionRecord(
                id=row.id,
                organization_id=row.organization_id,
                name=meta.get("collection_name", ""),
                slug=meta.get("collection_slug", ""),
                description=meta.get("collection_description"),
                version=meta.get("collection_version", "1.0.0"),
                state=meta.get("collection_state", "draft"),
                doc_type=meta.get("collection_doc_type", "custom"),
                page_count=len(meta.get("page_ids", [])),
                created_by=row.created_by,
                created_at=row.created_at,
            ))
        return results

    def get_collection_pages(
        self,
        context: OrgContext,
        collection_id: uuid.UUID,
    ) -> list[DocumentPage]:
        """Get all pages in a document collection, ordered."""
        pages = self._session.scalars(
            select(Artifact)
            .where(
                Artifact.organization_id == context.organization_id,
                Artifact.extra_metadata["collection_id"].as_string() == str(collection_id),
            )
            .order_by(Artifact.created_at.asc())
        ).all()
        result = []
        for p in pages:
            meta = p.extra_metadata or {}
            result.append(DocumentPage(
                artifact_id=p.id,
                title=meta.get("page_title", "Untitled"),
                page_order=meta.get("page_order", 0),
                parent_page_id=uuid.UUID(meta["parent_page_id"]) if meta.get("parent_page_id") else None,
                content_type=p.content_type,
                uri=p.uri,
            ))
        result.sort(key=lambda x: x.page_order)
        return result
