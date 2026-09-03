"""XIOFLOW WorkflowTemplate model — per-org isolated script library.

This is a scaffold. Full implementation in Phase 4.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_ts = TIMESTAMP(timezone=True)


class WorkflowTemplate(Base):
    """Per-org isolated script / workflow template library entry.

    organization_id=NULL means platform-global (available to all orgs).
    project_id=NULL means org-wide (not scoped to a specific project).
    """

    __tablename__ = "workflow_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        nullable=True,
        index=True,
    )  # NULL = platform-global
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    script_ref: Mapped[str] = mapped_column(Text, nullable=False)  # storage path
    is_platform_global: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[Any] = mapped_column(
        _ts, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[Any | None] = mapped_column(_ts)
