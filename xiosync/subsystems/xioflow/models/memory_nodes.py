"""XIOFLOW Memory Node — the core action memory graph.

Each row is a single recorded browser action with its 10-tier locator payload,
Bayesian consensus scores, device context hash, and DAG graph edges.

RLS: every row is scoped to ``organization_id``. Optionally scoped to
``project_id`` for project-level isolation within an org.

Tier Hierarchy (RLS-bounded):
    project_experimental → project_ground_truth →
    organization_shared → platform_global
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_ts = TIMESTAMP(timezone=True)


class XioflowMemoryNode(Base):
    """A single recorded browser action with 10-tier locator payload."""

    __tablename__ = "xioflow_memory_nodes"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        CheckConstraint(
            "tier IN ('project_experimental', 'project_ground_truth', "
            "'organization_shared', 'platform_global')",
            name="ck_xfmn_tier_allowed",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'ARCHIVED', 'DEPRECATED')",
            name="ck_xfmn_status_allowed",
        ),
        CheckConstraint(
            "action_type IN ('click', 'type', 'extract_data', 'scroll_down', "
            "'navigate', 'wait', 'done', 'trigger_sub_workflow', "
            "'compute_node', 'conditional')",
            name="ck_xfmn_action_type_allowed",
        ),
        CheckConstraint(
            "execution_mode IN ('sequential', 'parallel')",
            name="ck_xfmn_execution_mode_allowed",
        ),
        CheckConstraint(
            "volatility_type IN ('static', 'dynamic', 'bubble')",
            name="ck_xfmn_volatility_allowed",
        ),
        CheckConstraint(
            "recording_method IN ('auto_learn', 'teacher_extension', "
            "'declarative_dag', 'mcp_chat')",
            name="ck_xfmn_recording_method_allowed",
        ),
        Index("idx_xfmn_lookup", "lookup_key", "tier", "status"),
        Index("idx_xfmn_domain_intent", "domain", "intent"),
        Index("idx_xfmn_org", "organization_id"),
        Index("idx_xfmn_project", "project_id"),
        Index("idx_xfmn_context", "context_hash"),
    )

    # ── Identity ────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"),
    )

    # ── Memory Tier ─────────────────────────────────────────────────────
    tier: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'project_experimental'"),
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ACTIVE'"),
    )

    # ── Semantic Identity ───────────────────────────────────────────────
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Device Context Hash ─────────────────────────────────────────────
    context_hash: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'default'"),
    )
    device_type: Mapped[str | None] = mapped_column(Text)
    os_name: Mapped[str | None] = mapped_column(Text)
    browser: Mapped[str | None] = mapped_column(Text)
    viewport_width: Mapped[int | None] = mapped_column(Integer)
    viewport_height: Mapped[int | None] = mapped_column(Integer)

    # ── 10-Tier Locator Payload ─────────────────────────────────────────
    face_value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )
    place_value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )

    # ── Execution Definition ────────────────────────────────────────────
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    action_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )
    output_var: Mapped[str | None] = mapped_column(Text)
    execution_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'sequential'"),
    )

    # ── Graph Edges ─────────────────────────────────────────────────────
    previous_intent: Mapped[str | None] = mapped_column(Text)
    next_nodes: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default=text("'{}'"),
    )
    condition: Mapped[str | None] = mapped_column(
        Text, server_default=text("'default'"),
    )

    # ── Volatility & Plugin Binding ─────────────────────────────────────
    volatility_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'static'"),
    )
    fallback_plugin: Mapped[str | None] = mapped_column(Text)

    # ── Bayesian Consensus Scores ───────────────────────────────────────
    bayesian_score: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.5"),
    )
    ema_score: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.5"),
    )
    total_vote_weight: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0"),
    )
    promotions: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )
    ref_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )

    # ── Locator Priority Cache ──────────────────────────────────────────
    locator_priority: Mapped[list[int] | None] = mapped_column(
        ARRAY(Integer), server_default=text("'{1,2,3,4,5,6,7,8,9,10}'"),
    )

    # ── Provenance ──────────────────────────────────────────────────────
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.id"),
    )
    recording_method: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'auto_learn'"),
    )
    client_id: Mapped[str | None] = mapped_column(Text)

    # ── Timestamps ──────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()"),
    )
    last_used: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        _ts, nullable=False, server_default=text("now()"),
    )

    # ── Lookup Optimization ─────────────────────────────────────────────
    # NOTE: The Alembic migration creates lookup_key as a
    # GENERATED ALWAYS AS (domain || '::' || intent || '::' || context_hash) STORED
    # column. SQLAlchemy reads it but never writes to it.
    lookup_key: Mapped[str | None] = mapped_column(Text)
