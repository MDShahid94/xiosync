"""Usage metering model (Gap M-3).

Per-organization usage metrics aggregated by time period, supporting
consumption-based billing and cost allocation dashboards.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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


class UsageMeter(Base):
    """Per-organization usage metric (Gap M-3)."""

    __tablename__ = "usage_meters"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "period_start", "metric_type",
            name="uq_usage_meters_org_period_metric",
        ),
        Index("ix_usage_meters_org_metric_period", "organization_id", "metric_type", "period_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    period_start: Mapped[datetime] = mapped_column(_ts, nullable=False)
    period_end: Mapped[datetime] = mapped_column(_ts, nullable=False)
    metric_type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"),
    )
    meter_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()"),
    )
