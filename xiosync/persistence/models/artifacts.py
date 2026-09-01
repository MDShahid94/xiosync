"""Artifact reference model — provider-agnostic metadata (Gap D-1).

The platform stores artifact *references* only (URI, provider, content type,
size, checksum). It never proxies raw bytes; users choose their own storage
backend (R2, S3, GCS, Azure Blob, local, inline, custom).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_ts = TIMESTAMP(timezone=True)


class Artifact(Base):
    """Provider-agnostic artifact metadata reference (Gap D-1)."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_artifacts_created_by_same_org",
        ),
        CheckConstraint(
            "provider_type IN ('r2', 's3', 'gcs', 'azure_blob', 'local', 'inline', 'custom')",
            name="ck_artifacts_provider_type_allowed",
        ),
        Index("ix_artifacts_org_provider", "organization_id", "provider_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    provider_type: Mapped[str] = mapped_column(Text, nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(Text)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()")
    )  # IMM
