from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base

_timestamptz = TIMESTAMP(timezone=True)

class OrganizationBranding(Base):
    """Platform Branding Configuration for an organization."""

    __tablename__ = "organization_branding"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    theme_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="system", server_default="system"
    )
    primary_color: Mapped[str | None] = mapped_column(String, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    favicon_url: Mapped[str | None] = mapped_column(String, nullable=True)
    custom_domain: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)
