"""Capability, grant, and append-only authorization event models."""

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


class Capability(Base):
    __tablename__ = "capabilities"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "name"),
        # Gap X-2: blueprint column constraints.
        CheckConstraint(
            "execution_mode IN ('sync', 'async', 'streaming')",
            name="execution_mode_allowed",
        ),
        CheckConstraint(
            "state IN ('draft', 'active', 'deprecated')",
            name="state_allowed",
        ),
        CheckConstraint(
            "timeout_ms IS NULL OR timeout_ms > 0",
            name="timeout_ms_positive",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    # Gap X-2: capability blueprint columns.
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    execution_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'sync'")
    )
    timeout_ms: Mapped[int | None] = mapped_column()
    retry_policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))


class Grant(Base):
    __tablename__ = "grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "actor_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_grants_actor_same_org",
        ),
        ForeignKeyConstraint(
            ["organization_id", "capability_id"],
            ["capabilities.organization_id", "capabilities.id"],
            name="fk_grants_capability_same_org",
        ),
        CheckConstraint("state IN ('active', 'revoked')", name="state_allowed"),
        # Composite unique anchor — required by migration 0009's composite FK
        # from plugin_installations to grants(organization_id, id).
        UniqueConstraint("organization_id", "id", name="uq_grants_organization_id_id"),
        Index(
            "ix_grants_actor_capability_active",
            "organization_id",
            "actor_id",
            "capability_id",
            postgresql_where=text("state = 'active'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    capability_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    expires_at: Mapped[datetime | None] = mapped_column(_ts)
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(_ts)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_org_type_created", "organization_id", "event_type", "created_at"),
        # Gap R-4: indexes for first-class routing columns.
        CheckConstraint(
            "severity IN ('debug', 'info', 'warn', 'error', 'critical')",
            name="severity_allowed",
        ),
        Index("ix_events_org_type_severity", "organization_id", "event_type", "severity"),
        Index("ix_events_org_correlation", "organization_id", "correlation_id"),
        Index("ix_events_org_entity", "organization_id", "entity_type", "entity_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    # Gap R-4: first-class indexable routing columns.
    severity: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'info'")
    )
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
