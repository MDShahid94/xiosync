"""Operation — first-class record of actor activity (docs 03 §2.6, 06 §5).

An Operation is the provenance record of an actor's lifecycle transition or
meaningful activity (ported from XIOPATH's strongest design). Operations form
the *hierarchy* graph via ``parent_operation_id`` (doc 03 §3), which must stay
acyclic (INV-OP-1); that tree constraint is validated on write in the service
layer, not by the schema.

Same-org referential integrity (INV-TABLE-1 / INV-OP-2): ``actor_id``,
``initiated_by`` and the ``parent_operation_id`` self reference are composite
``(organization_id, <id>)`` FKs so every referenced actor/operation shares this
operation's organization.

``operation`` (an ``operation_type``) and ``from_state``/``to_state``
(``lifecycle_state`` values) are Type-Registry-validated data (doc 03 §8) and
get no CHECK. Only the closed sets doc 03 fixes in the schema — ``trigger``,
``scope`` and ``outcome`` — get value CHECKs.
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
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_timestamptz = TIMESTAMP(timezone=True)


class Operation(Base):
    """A first-class record of an actor's activity or transition (doc 03 §2.6)."""

    __tablename__ = "operations"
    __table_args__ = (
        # Anchor for the parent_operation_id same-org self reference.
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "actor_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_operations_actor_same_org",
        ),
        ForeignKeyConstraint(
            ["organization_id", "initiated_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_operations_initiated_by_same_org",
        ),
        # INV-OP-1: parent_operation_id forms an acyclic tree (validated on write).
        ForeignKeyConstraint(
            ["organization_id", "parent_operation_id"],
            ["operations.organization_id", "operations.id"],
            name="fk_operations_parent_same_org",
        ),
        CheckConstraint(
            "trigger IN ('user_command', 'schedule', 'auto', 'error', 'system')",
            name="trigger_allowed",
        ),
        CheckConstraint(
            "scope IN ('actor', 'component', 'organization')",
            name="scope_allowed",
        ),
        CheckConstraint(
            "outcome IN ('success', 'partial', 'failed', 'pending')",
            name="outcome_allowed",
        ),
        # Hot path: an actor's operations over time (doc 06 §7).
        Index("ix_operations_org_actor_started", "organization_id", "actor_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)  # operation_type (registry)
    from_state: Mapped[str | None] = mapped_column(Text)  # lifecycle_state (registry)
    to_state: Mapped[str | None] = mapped_column(Text)  # lifecycle_state (registry)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    initiated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    collaborators: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )  # [{actor_id, role_in_operation}]
    scope: Mapped[str | None] = mapped_column(Text)
    depth_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    parent_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    artifacts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rationale: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
    completed_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )
