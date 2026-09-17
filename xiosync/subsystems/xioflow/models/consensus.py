"""XIOFLOW Consensus Vote — Bayesian vote ledger.

Each row records a success/failure vote from an EdgeWorker (or user) against
a specific memory node. The ``ConsensusEngine`` aggregates these votes using
Bayesian EMA to automatically promote or demote memory nodes across tiers.

Unique constraint: one vote per (node, voter, context_hash) — prevents
spam-voting from a single worker.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Float,
    ForeignKey,
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


class XioflowConsensusVote(Base):
    """Bayesian vote ledger for tracking EdgeWorker success/failure per memory node."""

    __tablename__ = "xioflow_consensus_votes"
    __table_args__ = (
        UniqueConstraint(
            "node_id", "voter_id", "context_hash",
            name="uq_xfcv_node_voter_context",
        ),
        Index("idx_xfcv_org", "organization_id"),
        Index("idx_xfcv_node", "node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False,
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("xioflow_memory_nodes.id"), nullable=False,
    )
    voter_id: Mapped[str] = mapped_column(Text, nullable=False)
    raw_vote: Mapped[float] = mapped_column(Float, nullable=False)
    tier_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("1.0"),
    )
    winning_tier: Mapped[int | None] = mapped_column(Integer)
    context_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()"),
    )
