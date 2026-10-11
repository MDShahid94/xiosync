"""XIOFLOW Trigger model — cron and event trigger definitions."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_ts = TIMESTAMP(timezone=True)


class Trigger(Base):
    """Workflow trigger definition for cron and event automation."""

    __tablename__ = "xioflow_triggers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        nullable=False,
        index=True,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    cron_schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    context_defaults: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    filter_expression: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True,
        doc="JSONPath-like filter: trigger fires only when expression evaluates truthy against event payload"
    )
    last_fired_at: Mapped[Any | None] = mapped_column(_ts, nullable=True)
    created_at: Mapped[Any] = mapped_column(_ts, nullable=False, server_default=text("now()"))


XioflowTrigger = Trigger

__all__ = ["Trigger", "XioflowTrigger"]
