"""XIOFlow Agent models — first-class AI actors that reason, plan, and execute.

An Agent is a cognitive entity sandboxed within an organization (and optionally
a project) that can:
  1. Accept semantic goals and decompose them into plans
  2. Search memory nodes for existing trusted execution paths
  3. Dispatch DAG runs, scripts, or real-time browser exploration
  4. Learn from successful explorations and save them as templates
  5. Be governed via the RBAC capability system (same as human actors)

Each Agent has an associated Actor identity for unified RBAC, audit logging,
and cryptographic provenance.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.persistence.models.identity import new_id

_timestamptz = TIMESTAMP(timezone=True)


class XioflowAgent(Base):
    """A first-class AI agent scoped to an organization and optionally a project.

    Agents are the cognitive layer of XIOSYNC — they accept semantic directives
    ("sign into all Google accounts and verify 2FA") and autonomously decompose,
    plan, and execute workflows using the DAG memory graph.

    Architecture alignment:
      - Each agent has a dedicated ``actors`` row (``actor_type='agent'``)
        so it inherits RBAC capabilities, trust tiers, and audit trails.
      - ``project_id`` scopes the agent's memory access and execution rights.
      - ``capability_ids`` restricts what the agent can do (fine-grained RBAC).
      - ``tool_bindings`` defines which plugins/integrations the agent can invoke.
    """

    __tablename__ = "xioflow_agents"
    __table_args__ = (
        # Anchor for composite FKs from child tables (agent_runs, etc.)
        UniqueConstraint("organization_id", "id"),
        # Same-org actor binding — prevents cross-tenant agent-actor linkage
        ForeignKeyConstraint(
            ["organization_id", "actor_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_xioflow_agents_actor_same_org",
        ),
        # Agent type constraint
        CheckConstraint(
            "agent_type IN ("
            "'browser_automation', "  # Executes DAG workflows via Patchright
            "'data_extraction', "  # Reads/parses documents and web data
            "'system_admin', "  # Runs SSH commands, API calls, infra ops
            "'orchestrator', "  # Coordinates other agents (meta-agent)
            "'custom'"  # Plugin-defined behavior
            ")",
            name="ck_xfa_agent_type_allowed",
        ),
        # Memory scope constraint
        CheckConstraint(
            "memory_scope IN ('project', 'organization', 'global')",
            name="ck_xfa_memory_scope_allowed",
        ),
        # State constraint
        CheckConstraint(
            "state IN ("
            "'idle', "
            "'planning', "
            "'executing', "
            "'waiting_human', "
            "'errored', "
            "'suspended', "
            "'archived'"
            ")",
            name="ck_xfa_state_allowed",
        ),
        # Positive concurrency limit
        CheckConstraint(
            "max_concurrent_runs > 0",
            name="ck_xfa_max_concurrent_positive",
        ),
        # Hot path: list agents by org + type + state
        Index(
            "ix_xioflow_agents_org_type_state",
            "organization_id",
            "agent_type",
            "state",
        ),
        # Unique slug within org
        UniqueConstraint(
            "organization_id",
            "slug",
            name="uq_xioflow_agents_org_slug",
        ),
    )

    # ── Identity ─────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # ── Type & RBAC ──────────────────────────────────────────────────────
    agent_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'browser_automation'")
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    capability_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        server_default=text("'{}'"),
    )

    # ── Configuration ────────────────────────────────────────────────────
    model_config_json: Mapped[dict[str, Any]] = mapped_column(
        "model_config",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    tool_bindings: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    system_prompt: Mapped[str | None] = mapped_column(Text)
    memory_scope: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'project'"),
    )

    # ── Runtime State ────────────────────────────────────────────────────
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'idle'"),
    )
    max_concurrent_runs: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    last_run_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    total_runs: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    total_successes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    total_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    # ── Timestamps ───────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    deleted_at: Mapped[datetime | None] = mapped_column(_timestamptz)


class XioflowAgentRun(Base):
    """A single execution of an agent's cognitive loop against a semantic goal.

    The agent decomposes the goal into a plan, searches memory for existing
    paths, dispatches DAG runs, and reports results. Each agent_run tracks
    the full lifecycle from goal intake to completion.
    """

    __tablename__ = "xioflow_agent_runs"
    __table_args__ = (
        # Same-org agent binding
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["xioflow_agents.organization_id", "xioflow_agents.id"],
            name="fk_xioflow_agent_runs_agent_same_org",
        ),
        # State constraint
        CheckConstraint(
            "state IN ("
            "'PENDING', "
            "'PLANNING', "
            "'EXECUTING', "
            "'WAITING_HUMAN', "
            "'SUCCESS', "
            "'FAILED', "
            "'CANCELLED'"
            ")",
            name="ck_xfar_state_allowed",
        ),
        # Hot path: agent's runs over time
        Index(
            "ix_xioflow_agent_runs_agent_state",
            "agent_id",
            "state",
        ),
        Index(
            "ix_xioflow_agent_runs_org_started",
            "organization_id",
            "started_at",
        ),
    )

    # ── Identity ─────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        nullable=False,
        index=True,
    )

    # ── Goal & Plan ──────────────────────────────────────────────────────
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    plan_steps_total: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    plan_steps_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # ── Execution State ──────────────────────────────────────────────────
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'PENDING'"),
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    dag_run_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        server_default=text("'{}'"),
    )
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    error: Mapped[str | None] = mapped_column(Text)

    # ── Timestamps ───────────────────────────────────────────────────────
    started_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(_timestamptz)
