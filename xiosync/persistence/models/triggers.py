"""Workflow trigger model (Gap R-5).

Supports cron schedules, event-driven triggers, and incoming webhook triggers
for automated workflow execution.
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


class WorkflowTrigger(Base):
    """Workflow trigger definition (Gap R-5)."""

    __tablename__ = "workflow_triggers"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "workflow_id"],
            ["workflows.organization_id", "workflows.id"],
            name="fk_workflow_triggers_workflow_same_org",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_workflow_triggers_created_by_same_org",
        ),
        CheckConstraint(
            "trigger_type IN ('cron', 'event', 'webhook')",
            name="ck_workflow_triggers_type_allowed",
        ),
        CheckConstraint(
            "state IN ('active', 'paused', 'disabled')",
            name="ck_workflow_triggers_state_allowed",
        ),
        Index("ix_workflow_triggers_org_state", "organization_id", "state"),
        Index(
            "ix_workflow_triggers_next_fire",
            "next_fire_at",
            postgresql_where=text("state = 'active' AND trigger_type = 'cron'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )  # IMM
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'"),
    )
    last_fired_at: Mapped[datetime | None] = mapped_column(_ts)
    next_fire_at: Mapped[datetime | None] = mapped_column(_ts)
    created_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()"),
    )  # IMM
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
