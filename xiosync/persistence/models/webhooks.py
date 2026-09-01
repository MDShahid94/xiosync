"""Webhook subscription model (Gap R-2).

Outbound webhook subscriptions for event-driven integration. The platform
generates a signing secret per subscription and delivers events by POST
to the configured URL, signing the payload with HMAC-SHA256.
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_ts = TIMESTAMP(timezone=True)


class WebhookSubscription(Base):
    """Outbound webhook subscription (Gap R-2)."""

    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        CheckConstraint(
            "state IN ('active', 'paused', 'disabled')",
            name="ck_webhook_subs_state_allowed",
        ),
        Index("ix_webhook_subs_org_state", "organization_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )  # IMM
    url: Mapped[str] = mapped_column(Text, nullable=False)
    event_types: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    signing_secret: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()")
    )  # IMM
    updated_at: Mapped[datetime | None] = mapped_column(_ts)
