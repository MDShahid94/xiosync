"""Enterprise document management service (rewritten for proper tables).

Uses ``document_collections`` and ``document_pages`` tables instead of
JSONB-in-artifacts. Supports multi-page, hierarchical, versioned
document collections with full lifecycle governance.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.documents import DocumentCollection, DocumentPage
from xiosync.persistence.models.operations import Operation
from xiosync.platform.ids import new_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PageRecord:
    """Frozen snapshot of a document page."""
    id: uuid.UUID
    collection_id: uuid.UUID
    artifact_id: uuid.UUID | None
    title: str
    slug: str
    content_format: str
    inline_content: str | None
    page_order: int
    parent_page_id: uuid.UUID | None
    depth: int


@dataclass(frozen=True, slots=True)
class CollectionRecord:
    """Frozen snapshot of a document collection."""
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    version: str
    state: str
    doc_type: str
    page_count: int
    created_by: uuid.UUID
    created_at: datetime


def _collection_record(row: DocumentCollection, page_count: int = 0) -> CollectionRecord:
    return CollectionRecord(
        id=row.id, organization_id=row.organization_id, name=row.name,
        slug=row.slug, description=row.description, version=row.version,
        state=row.state, doc_type=row.doc_type, page_count=page_count,
        created_by=row.created_by, created_at=row.created_at,
    )


def _page_record(row: DocumentPage) -> PageRecord:
    return PageRecord(
        id=row.id, collection_id=row.collection_id, artifact_id=row.artifact_id,
        title=row.title, slug=row.slug, content_format=row.content_format,
        inline_content=row.inline_content, page_order=row.page_order,
        parent_page_id=row.parent_page_id, depth=row.depth,
    )


class DocumentNotFoundError(ValueError):
    def __init__(self, doc_id: uuid.UUID) -> None:
        super().__init__(f"Document collection {doc_id} not found")


class DocumentService:
    """Enterprise document management on proper tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_collection(
        self, context: OrgContext, *, name: str, slug: str,
        doc_type: str = "custom", description: str | None = None,
        version: str = "1.0.0",
    ) -> CollectionRecord:
        now = datetime.now(UTC)
        coll = DocumentCollection(
            id=new_id(), organization_id=context.organization_id,
            name=name, slug=slug, doc_type=doc_type, description=description,
            version=version, state="draft", created_by=context.actor_id,
            created_at=now,
        )
        self._session.add(coll)

        op_id = new_id()
        self._session.add(Operation(
            id=op_id, organization_id=context.organization_id,
            actor_id=context.actor_id, operation="document.collection.create",
            trigger="user_command", initiated_by=context.actor_id,
            scope="organization", outcome="success",
            rationale=f"Created document collection: {name}",
            started_at=now, completed_at=now,
        ))
        self._session.add(Event(
            id=new_id(), organization_id=context.organization_id,
            event_type="artifact.created", actor_id=context.actor_id,
            severity="info", operation_id=op_id,
            entity_type="document_collection", entity_id=coll.id,
            payload={"name": name, "slug": slug, "doc_type": doc_type, "version": version},
            created_at=now,
        ))
        self._session.flush()
        return _collection_record(coll, 0)

    def add_page(
        self, context: OrgContext, *, collection_id: uuid.UUID,
        title: str, slug: str, content_format: str = "markdown",
        inline_content: str | None = None, artifact_id: uuid.UUID | None = None,
        page_order: int | None = None, parent_page_id: uuid.UUID | None = None,
    ) -> PageRecord:
        coll = self._session.scalar(
            select(DocumentCollection).where(
                DocumentCollection.id == collection_id,
                DocumentCollection.organization_id == context.organization_id,
            )
        )
        if coll is None:
            raise DocumentNotFoundError(collection_id)

        if page_order is None:
            count = self._session.scalar(
                select(func.count()).select_from(DocumentPage)
                .where(DocumentPage.collection_id == collection_id)
            ) or 0
            page_order = count + 1

        depth = 0
        if parent_page_id:
            parent = self._session.scalar(
                select(DocumentPage).where(DocumentPage.id == parent_page_id)
            )
            if parent:
                depth = parent.depth + 1

        page = DocumentPage(
            id=new_id(), collection_id=collection_id,
            organization_id=context.organization_id,
            artifact_id=artifact_id, title=title, slug=slug,
            content_format=content_format, inline_content=inline_content,
            page_order=page_order, parent_page_id=parent_page_id, depth=depth,
        )
        self._session.add(page)
        self._session.flush()
        return _page_record(page)

    def publish_collection(
        self, context: OrgContext, collection_id: uuid.UUID, *,
        new_version: str | None = None,
    ) -> CollectionRecord:
        coll = self._session.scalar(
            select(DocumentCollection).where(
                DocumentCollection.id == collection_id,
                DocumentCollection.organization_id == context.organization_id,
            )
        )
        if coll is None:
            raise DocumentNotFoundError(collection_id)

        now = datetime.now(UTC)
        old_state = coll.state
        updates: dict[str, Any] = {"state": "published", "published_at": now, "updated_at": now}
        if new_version:
            updates["version"] = new_version
        self._session.execute(
            update(DocumentCollection).where(DocumentCollection.id == collection_id).values(**updates)
        )

        op_id = new_id()
        self._session.add(Operation(
            id=op_id, organization_id=context.organization_id,
            actor_id=context.actor_id, operation="document.collection.publish",
            trigger="user_command", initiated_by=context.actor_id,
            scope="organization", outcome="success",
            from_state=old_state, to_state="published",
            rationale=f"Published: {coll.name}",
            started_at=now, completed_at=now,
        ))
        self._session.add(Event(
            id=new_id(), organization_id=context.organization_id,
            event_type="artifact.created", actor_id=context.actor_id,
            severity="info", operation_id=op_id,
            entity_type="document_collection", entity_id=collection_id,
            payload={"action": "published", "version": new_version or coll.version},
            created_at=now,
        ))
        self._session.flush()

        page_count = self._session.scalar(
            select(func.count()).select_from(DocumentPage)
            .where(DocumentPage.collection_id == collection_id)
        ) or 0
        # Refresh to get updated values
        self._session.expire(coll)
        coll_fresh = self._session.scalar(
            select(DocumentCollection).where(DocumentCollection.id == collection_id)
        )
        assert coll_fresh is not None
        return _collection_record(coll_fresh, page_count)

    def list_collections(
        self, context: OrgContext, *, doc_type: str | None = None,
        state: str | None = None, limit: int = 50,
    ) -> list[CollectionRecord]:
        stmt = (
            select(DocumentCollection)
            .where(DocumentCollection.organization_id == context.organization_id)
            .order_by(DocumentCollection.created_at.desc())
            .limit(limit)
        )
        if doc_type:
            stmt = stmt.where(DocumentCollection.doc_type == doc_type)
        if state:
            stmt = stmt.where(DocumentCollection.state == state)
        rows = self._session.scalars(stmt).all()
        results = []
        for row in rows:
            pc = self._session.scalar(
                select(func.count()).select_from(DocumentPage)
                .where(DocumentPage.collection_id == row.id)
            ) or 0
            results.append(_collection_record(row, pc))
        return results

    def get_collection_pages(
        self, context: OrgContext, collection_id: uuid.UUID,
    ) -> list[PageRecord]:
        pages = self._session.scalars(
            select(DocumentPage)
            .where(
                DocumentPage.collection_id == collection_id,
                DocumentPage.organization_id == context.organization_id,
            )
            .order_by(DocumentPage.page_order)
        ).all()
        return [_page_record(p) for p in pages]

    def get_page(
        self, context: OrgContext, page_id: uuid.UUID,
    ) -> PageRecord | None:
        row = self._session.scalar(
            select(DocumentPage).where(
                DocumentPage.id == page_id,
                DocumentPage.organization_id == context.organization_id,
            )
        )
        return _page_record(row) if row else None

    def generate_llms_txt(
        self, context: OrgContext, collection_id: uuid.UUID,
    ) -> str:
        """Generate llms.txt index for a document collection."""
        coll = self._session.scalar(
            select(DocumentCollection).where(
                DocumentCollection.id == collection_id,
                DocumentCollection.organization_id == context.organization_id,
            )
        )
        if coll is None:
            raise DocumentNotFoundError(collection_id)

        pages = self.get_collection_pages(context, collection_id)

        lines = [f"# {coll.name}", ""]
        if coll.description:
            lines.append(f"> {coll.description}")
            lines.append("")

        # Group by top-level sections (depth=0 parents)
        top_level = [p for p in pages if p.parent_page_id is None]
        children: dict[uuid.UUID, list[PageRecord]] = {p.id: [] for p in top_level}
        for p in pages:
            if p.parent_page_id and p.parent_page_id in children:
                children[p.parent_page_id].append(p)

        for section in top_level:
            if children.get(section.id):
                lines.append(f"## {section.title}")
                for child in children[section.id]:
                    lines.append(f"- [{child.title}](/api/v1/docs/{coll.slug}/pages/{child.slug}.md): {child.title}")
                lines.append("")
            else:
                lines.append(f"- [{section.title}](/api/v1/docs/{coll.slug}/pages/{section.slug}.md): {section.title}")

        return "\n".join(lines) + "\n"

    def generate_llms_full_txt(
        self, context: OrgContext, collection_id: uuid.UUID,
    ) -> str:
        """Generate llms-full.txt — all pages concatenated."""
        coll = self._session.scalar(
            select(DocumentCollection).where(
                DocumentCollection.id == collection_id,
                DocumentCollection.organization_id == context.organization_id,
            )
        )
        if coll is None:
            raise DocumentNotFoundError(collection_id)

        pages = self.get_collection_pages(context, collection_id)

        lines = [
            "---",
            f"title: {coll.name}",
            f"version: {coll.version}",
            f"page_count: {len(pages)}",
            f"generated_at: {datetime.now(UTC).isoformat()}",
            "---",
            "",
        ]

        for page in pages:
            lines.append(f"# {page.title}")
            lines.append("")
            if page.inline_content:
                lines.append(page.inline_content)
            else:
                lines.append(f"*Content at: artifact {page.artifact_id}*")
            lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)
