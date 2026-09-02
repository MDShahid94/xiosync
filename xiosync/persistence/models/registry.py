"""Registry categories & capability groups ORM models (Genesis Phase 0).

``RegistryCategory`` — self-extensible category registry for the type_registry.
Instead of a hardcoded CHECK constraint, valid categories are rows in this
table.  New concept categories can be registered at runtime.

``CapabilityGroup`` — configurable RBAC groups that expand to fine-grained
capability operations.  Route handlers check against a group name; the group
definition maps to specific operations and is tenant-configurable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_timestamptz = TIMESTAMP(timezone=True)


class RegistryCategory(Base):
    """Self-extensible registry of valid type_registry categories.

    Replaces the hardcoded CHECK constraint on ``type_registry.category`` (Gap
    G-6).  Categories themselves are now a registrable concept — the
    meta-registry.
    """

    __tablename__ = "registry_categories"
    __table_args__ = (
        UniqueConstraint("name", name="uq_registry_categories_name"),
        CheckConstraint(
            "state IN ('active', 'deprecated')",
            name="ck_registry_categories_state_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'core'")
    )
    description: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )


class CapabilityGroup(Base):
    """Configurable RBAC group mapping a name to fine-grained operations.

    Route handlers check ``require_capability("group_name")``.  The group
    expands to a list of operations (e.g. ``["workflow.create",
    "workflow.publish"]``).  Groups with ``organization_id IS NULL`` are
    platform-global defaults; org-scoped groups override for that tenant.
    """

    __tablename__ = "capability_groups"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_capability_groups_org_name"),
        CheckConstraint(
            "state IN ('active', 'deprecated')",
            name="ck_capability_groups_state_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    operations: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)
