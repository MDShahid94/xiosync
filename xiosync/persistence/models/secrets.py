"""Secret reference model (Gap S-1).

Provider-agnostic secret references. The platform stores metadata only
(provider type, config path/ARN/var name); actual secret values are
resolved by provider adapters at the worker side.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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


class SecretRef(Base):
    """Provider-agnostic secret reference (Gap S-1)."""

    __tablename__ = "secret_refs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "name", name="uq_secret_refs_org_name"),
        ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_secret_refs_created_by_same_org",
        ),
        CheckConstraint(
            "provider IN ('env', 'vault', 'aws-sm', 'gcp-sm', 'azure-kv', 'inline', 'custom')",
            name="ck_secret_refs_provider_allowed",
        ),
        CheckConstraint(
            "state IN ('active', 'rotated', 'revoked')",
            name="ck_secret_refs_state_allowed",
        ),
        Index("ix_secret_refs_org_state", "organization_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )  # IMM
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    ref_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()"),
    )  # IMM
    rotated_at: Mapped[datetime | None] = mapped_column(_ts)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
