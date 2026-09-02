"""Document collection and page ORM models (improvement #2)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_ts = TIMESTAMP(timezone=True)


class DocumentCollection(Base):
    """A governed, versioned document collection."""

    __tablename__ = "document_collections"
    __table_args__ = (
        CheckConstraint(
            "state IN ('draft','published','archived','deprecated')",
            name="ck_doc_collections_state",
        ),
        UniqueConstraint(
            "organization_id", "slug", "version",
            name="uq_doc_collections_org_slug_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="custom")
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text, nullable=False, server_default="1.0.0")
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(_ts)
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime | None] = mapped_column(_ts)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )

    pages: Mapped[list[DocumentPage]] = relationship(
        back_populates="collection", cascade="all, delete-orphan",
        order_by="DocumentPage.page_order",
    )


class DocumentPage(Base):
    """A single page within a document collection."""

    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("collection_id", "slug", name="uq_doc_pages_collection_slug"),
        Index("ix_doc_pages_parent", "collection_id", "parent_page_id"),
        Index("ix_doc_pages_order", "collection_id", "page_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_collections.id", ondelete="CASCADE"), nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id"),
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    content_format: Mapped[str] = mapped_column(Text, nullable=False, server_default="markdown")
    inline_content: Mapped[str | None] = mapped_column(Text)
    page_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    parent_page_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_pages.id"),
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime | None] = mapped_column(_ts)

    collection: Mapped[DocumentCollection] = relationship(back_populates="pages")
