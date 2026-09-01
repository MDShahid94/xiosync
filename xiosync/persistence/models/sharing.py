"""Resource share model (Gap M-2).

Cross-organization resource sharing. The ``resource_shares`` table does NOT
have RLS — it is read by RLS policies on other shareable tables. Access
control is enforced at the service layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_ts = TIMESTAMP(timezone=True)


class ResourceShare(Base):
    """Cross-organization resource share (Gap M-2)."""

    __tablename__ = "resource_shares"
    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('capability', 'artifact', 'workflow', 'plugin')",
            name="ck_resource_shares_type_allowed",
        ),
        CheckConstraint(
            "state IN ('active', 'revoked')",
            name="ck_resource_shares_state_allowed",
        ),
        Index("ix_resource_shares_source_org", "source_org_id"),
        Index("ix_resource_shares_target_org", "target_org_id"),
        Index("ix_resource_shares_resource", "resource_type", "resource_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    source_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False,
    )
    target_org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True,
    )  # NULL = public/global share
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[\"read\"]'::jsonb"),
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()"),
    )
    expires_at: Mapped[datetime | None] = mapped_column(_ts)
