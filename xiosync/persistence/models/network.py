"""Worker network allowlist model (Gap S-2).

Extends the plugin-only ``plugin_network_allow_rules`` pattern to worker
compute nodes, allowing per-worker egress restrictions.
"""

from __future__ import annotations

import uuid
from datetime import datetime

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
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_ts = TIMESTAMP(timezone=True)


class WorkerNetworkAllowRule(Base):
    """Per-worker egress allowlist rule (Gap S-2)."""

    __tablename__ = "worker_network_allow_rules"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "enrollment_id"],
            ["worker_enrollments.organization_id", "worker_enrollments.id"],
            name="fk_worker_net_rules_enrollment_same_org",
        ),
        CheckConstraint(
            "port > 0 AND port <= 65535",
            name="ck_worker_net_rules_port_range",
        ),
        CheckConstraint(
            "protocol IN ('http', 'https', 'wss')",
            name="ck_worker_net_rules_protocol_allowed",
        ),
        Index("ix_worker_net_rules_org_enrollment", "organization_id", "enrollment_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )  # IMM
    enrollment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    host_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'https'")
    )
    created_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()")
    )  # IMM
