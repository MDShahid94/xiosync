"""Workflow, WorkflowRun, Task & DeadLetter tables (docs 03 §§2.11, 4.4; 06 §5; 07).

Four models form the durable-execution spine (doc 04 §1: the control plane
authorizes and schedules; the execution plane only leases):

* ``Workflow`` — a versioned DAG *definition* (doc 03 §2.11). Its ``spec`` is a
  workflow-class graph that MUST be acyclic before it may be ``published``
  (INV-WF-1); acyclicity is validated on publish in the service layer (the
  schema cannot express it). Promotion of a corrected spec is a *new version*
  (INV-DLQ-4 / doc 06 §6), never an in-place edit — modelled as a new row.
* ``WorkflowRun`` — one execution of a Workflow (doc 03 §§2.11, 4.4).
* ``Task`` — a single leaseable unit of a run (doc 07 §1.1). Carries the lease
  protocol fields (``lease_id``, ``leased_by``, ``lease_expires_at``) that are
  the *only* channel between the control and execution planes (INV-EXEC-1).
* ``DeadLetter`` — a failed task whose retries are exhausted (doc 07 §4). It
  lands in state ``open`` and **nothing auto-resolves it** (INV-DLQ-1); the
  self-learning engine may attach a diagnosis/proposal but never flips this row
  to resolved (INV-DLQ-2). ``proposal_id`` is a soft pointer to a future
  proposal record (that table arrives with the governed-correction phase); it
  is intentionally *not* a foreign key yet.

Same-org referential integrity (INV-TABLE-1 / INV-TENANT-4, doc 05): every FK
to a tenant-bearing row is a composite ``(organization_id, <id>)`` reference so
a cross-org link cannot be committed. ``created_by``/``initiated_by`` reference
``actors``; ``workflow_runs.workflow_id`` references ``workflows``;
``tasks.run_id`` references ``workflow_runs``; ``tasks.capability_id`` references
``capabilities``; ``tasks.leased_by`` references the worker ``actors`` row; and
``dead_letters.task_id`` references ``tasks`` — all within the same org.

Closed state sets (doc 03 §§2.11, 4.4; doc 07 §1.1) get value CHECKs because
they are schema-fixed, mirroring the ``domain/workflows`` frozensets. The
``spec`` DAG contents are *not* CHECK-able and are validated on publish instead.
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
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_timestamptz = TIMESTAMP(timezone=True)


class Workflow(Base):
    """A versioned, DAG-defined workflow definition (doc 03 §2.11)."""

    __tablename__ = "workflows"
    __table_args__ = (
        # Anchor for the workflow_runs composite same-org FK.
        UniqueConstraint("organization_id", "id"),
        # A workflow's (name, version) is unique within its org; a promoted
        # correction is a new version row (doc 06 §6, INV-DLQ-4).
        UniqueConstraint(
            "organization_id",
            "name",
            "version",
            name="uq_workflows_organization_id_name_version",
        ),
        # created_by is an actor in the same org (INV-TABLE-1).
        ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_workflows_created_by_same_org",
        ),
        # INV-WF-1: 'published' requires a validated DAG (checked on write in
        # the service layer); the closed lifecycle set is fixed here.
        CheckConstraint(
            "state IN ('draft', 'published', 'deprecated')",
            name="state_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )  # the DAG definition (nodes/edges); validated on publish (INV-WF-1)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
    updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)


class WorkflowRun(Base):
    """A single execution of a Workflow (doc 03 §§2.11, 4.4)."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        # Anchor for the tasks composite same-org FK.
        UniqueConstraint("organization_id", "id"),
        # INV-TABLE-1: the run's workflow and initiator are in the run's org.
        ForeignKeyConstraint(
            ["organization_id", "workflow_id"],
            ["workflows.organization_id", "workflows.id"],
            name="fk_workflow_runs_workflow_same_org",
        ),
        ForeignKeyConstraint(
            ["organization_id", "initiated_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_workflow_runs_initiated_by_same_org",
        ),
        CheckConstraint(
            "state IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled')",
            name="state_allowed",
        ),
        # Hot path: a workflow's runs (doc 06 §7).
        Index("ix_workflow_runs_org_workflow", "organization_id", "workflow_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    initiated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    finished_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM


class Task(Base):
    """A single leaseable unit of a WorkflowRun (doc 07 §1.1)."""

    __tablename__ = "tasks"
    __table_args__ = (
        # Anchor for the dead_letters composite same-org FK.
        UniqueConstraint("organization_id", "id"),
        # INV-TABLE-1: run, capability and (when set) the leasing worker are
        # all in the task's org.
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_tasks_run_same_org",
        ),
        ForeignKeyConstraint(
            ["organization_id", "capability_id"],
            ["capabilities.organization_id", "capabilities.id"],
            name="fk_tasks_capability_same_org",
        ),
        ForeignKeyConstraint(
            ["organization_id", "leased_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_tasks_leased_by_same_org",
        ),
        CheckConstraint(
            "state IN ('queued', 'leased', 'completed', 'failed', 'expired', 'dead_letter')",
            name="state_allowed",
        ),
        # doc 06 §7 / §157: lease-sweep hot path — expiring stale leases scans
        # (organization_id, state, lease_expires_at).
        Index(
            "ix_tasks_org_state_lease_expires",
            "organization_id",
            "state",
            "lease_expires_at",
        ),
        # Gap X-3: priority range constraint (0 = lowest, 10 = highest).
        CheckConstraint(
            "priority >= 0 AND priority <= 10",
            name="ck_tasks_priority_range",
        ),
        # Gap X-3: descending-priority dispatch index for future queue polling.
        Index(
            "ix_tasks_org_priority_state",
            "organization_id",
            text("priority DESC"),
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)  # node key within the workflow spec
    capability_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Lease protocol (doc 04 §3.1 / doc 07 §1.1): the only control<->execution
    # channel. All null while queued; set atomically on lease, cleared on return.
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    leased_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # worker actor
    lease_expires_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # untrusted until validated
    # Gap T-1: task input parameters delivered to the worker at lease time.
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Gap T-4: incremental progress snapshot updated via heartbeat.
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    progress_updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    # Gap X-3: scheduling priority (0 = lowest, 10 = highest, default = 5).
    priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("5")
    )
    # Gap W-3: task checkpointing for state recovery after worker crash.
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    checkpoint_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM


class DeadLetter(Base):
    """A failed task with exhausted retries, awaiting governed correction (doc 07 §4)."""

    __tablename__ = "dead_letters"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        # INV-TABLE-1: the failed task is in this record's org.
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_dead_letters_task_same_org",
        ),
        # INV-DLQ-1: lands 'open'; advancing to 'resolved' is governed
        # (INV-DLQ-2/3), never a model side effect.
        CheckConstraint(
            "state IN ('open', 'investigating', 'resolved')",
            name="state_allowed",
        ),
        # Hot path: open dead letters awaiting triage (doc 06 §7).
        Index("ix_dead_letters_org_state", "organization_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # self-learning output (advisory)
    diagnosis: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Soft pointer to a future proposal record (INV-DLQ-2/3). Deliberately NOT a
    # FK: the proposals table arrives with the governed-correction phase.
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    # Gap T-5: rich failure context for diagnosis and recovery.
    stack_trace: Mapped[str | None] = mapped_column(Text)
    last_checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    original_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    dl_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    attempts: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
    updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)
