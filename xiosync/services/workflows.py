"""Workflow / run / task / dead-letter use cases (docs 03 §§2.11, 4.4; 07).

``WorkflowService`` is the sanctioned entry point for the durable-execution
spine. It orchestrates the pure DAG predicate (``domain/workflows``) over the
tenant-scoped ORM models, enforcing on write:

* **INV-WF-1 — a workflow may only be published with a valid DAG (doc 03 §3).**
  ``publish_workflow`` runs ``validate_workflow_dag`` over the stored ``spec``
  *before* flipping the row to ``published``; a cyclic or malformed spec is
  rejected with a domain error and the row stays ``draft``. The schema cannot
  express acyclicity, so this service is the gate.
* **INV-DLQ-1 — a failed task lands in ``dead_letters`` in state ``open`` and
  nothing auto-resolves it (doc 07 §4).** ``dead_letter_task`` moves the task to
  the terminal ``dead_letter`` state and inserts the dead-letter record in the
  landing state named by the domain (``DEAD_LETTER_LANDING_STATE``); advancing
  it to ``resolved`` is a later, governed act (INV-DLQ-2/3), never done here.

This is an intentionally thin "stub" service for Phase 3 Step 1: it covers the
create/publish/run/enqueue/dead-letter surface the invariants are proven
against and will grow the lease protocol (doc 04 §3.1) in a later step. It
follows the ``EventService`` shape — it takes the caller's ``Session`` directly
(the caller owns the transaction via ``org_scoped_session``), flushes each write
within it, and returns frozen record values so the service holds no live ORM
state. ``organization_id`` always comes from the context, never the caller.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.domain.workflows import (
    DEAD_LETTER_LANDING_STATE,
    DEAD_LETTER_STATE_INVESTIGATING,
    DEAD_LETTER_STATE_RESOLVED,
    TASK_STATE_COMPLETED,
    TASK_STATE_DEAD_LETTER,
    TASK_STATE_LEASED,
    TASK_STATE_QUEUED,
    WORKFLOW_RUN_STATE_QUEUED,
    WORKFLOW_STATE_DRAFT,
    WORKFLOW_STATE_PUBLISHED,
    WorkflowCycleError,
    WorkflowSpecError,
    dead_letter_accepts_proposal,
    dead_letter_is_approvable,
    lease_has_expired,
    task_is_completable,
    task_is_completed,
    task_is_leaseable,
    validate_workflow_dag,
)
from xiosync.persistence.models.workflows import (
    DeadLetter,
    Task,
    Workflow,
    WorkflowRun,
)
from xiosync.platform.ids import new_id

__all__ = [
    "CapabilityMismatchError",
    "CompletionOutcome",
    "DeadLetterNotFoundError",
    "DeadLetterRecord",
    "InactiveLeaseError",
    "NonCompletableError",
    "TaskNotFoundError",
    "TaskRecord",
    "UnleaseableError",
    "WorkflowCycleError",
    "WorkflowNotFoundError",
    "WorkflowRecord",
    "WorkflowRunRecord",
    "WorkflowService",
    "WorkflowSpecError",
]


class WorkflowNotFoundError(Exception):
    """The referenced workflow does not exist in this organization."""

    def __init__(self, workflow_id: uuid.UUID) -> None:
        super().__init__(f"workflow {workflow_id} not found in organization")
        self.workflow_id = workflow_id


class TaskNotFoundError(Exception):
    """The referenced task does not exist in this organization."""

    def __init__(self, task_id: uuid.UUID) -> None:
        super().__init__(f"task {task_id} not found in organization")
        self.task_id = task_id


class UnleaseableError(Exception):
    """A task cannot be leased because it is not in the ``queued`` state."""

    def __init__(self, task_id: uuid.UUID, state: str) -> None:
        super().__init__(f"task {task_id} is not leaseable (state={state!r})")
        self.task_id = task_id
        self.state = state


class InactiveLeaseError(Exception):
    """The lease is expired or the caller's lease_id does not match."""

    def __init__(self, task_id: uuid.UUID, reason: str = "lease inactive") -> None:
        super().__init__(f"task {task_id}: {reason}")
        self.task_id = task_id


class NonCompletableError(Exception):
    """A task cannot be completed because it is not in the ``leased`` state."""

    def __init__(self, task_id: uuid.UUID, state: str) -> None:
        super().__init__(f"task {task_id} cannot be completed (state={state!r})")
        self.task_id = task_id
        self.state = state


class DeadLetterNotFoundError(Exception):
    """The referenced dead-letter record does not exist in this organization."""

    def __init__(self, dead_letter_id: uuid.UUID) -> None:
        super().__init__(f"dead_letter {dead_letter_id} not found in organization")
        self.dead_letter_id = dead_letter_id


class CapabilityMismatchError(Exception):
    """Worker's capability manifest does not satisfy the task's requirement."""

    def __init__(self, task_id: uuid.UUID, required: str) -> None:
        super().__init__(
            f"task {task_id} requires capability {required!r} "
            "which the worker's manifest does not satisfy"
        )
        self.task_id = task_id
        self.required = required



@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    """One ``workflows`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    version: int
    spec: dict[str, Any]
    state: str
    created_by: uuid.UUID


@dataclass(frozen=True, slots=True)
class WorkflowRunRecord:
    """One ``workflow_runs`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    workflow_id: uuid.UUID
    state: str
    initiated_by: uuid.UUID


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """One ``tasks`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    run_id: uuid.UUID
    node_id: str
    capability_id: uuid.UUID
    state: str
    attempts: int
    # Lease fields (None when the task has not been leased yet)
    lease_id: uuid.UUID | None
    leased_by: uuid.UUID | None   # FK → actors.id (the worker actor)
    lease_expires_at: datetime | None
    # Gap T-1: task input parameters (None when not set)
    input: dict[str, Any] | None
    # Gap T-4: incremental progress snapshot
    progress: dict[str, Any] | None
    progress_updated_at: datetime | None
    # Gap X-3: scheduling priority (0 = lowest, 10 = highest)
    priority: int
    # Gap W-3: checkpoint for state recovery after worker crash
    checkpoint: dict[str, Any] | None
    checkpoint_at: datetime | None


@dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    """One ``dead_letters`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    task_id: uuid.UUID
    state: str
    failure_reason: str | None
    proposal_id: uuid.UUID | None
    diagnosis: dict[str, Any] | None
    # Gap T-5: rich failure context for diagnosis and recovery.
    stack_trace: str | None
    last_checkpoint: dict[str, Any] | None
    original_input: dict[str, Any] | None
    dl_metadata: dict[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    """Result of ``complete_task`` — carries an idempotency flag.

    ``duplicate=True`` means the task was already ``completed`` before this
    call; the caller should treat the outcome as a no-op rather than an error
    (INV-EXEC-2).
    """

    task_id: uuid.UUID
    state: str
    result: dict[str, Any] | None
    duplicate: bool


def _workflow_record(row: Workflow) -> WorkflowRecord:
    return WorkflowRecord(
        id=row.id,
        organization_id=row.organization_id,
        name=row.name,
        version=row.version,
        spec=row.spec,
        state=row.state,
        created_by=row.created_by,
    )


def _run_record(row: WorkflowRun) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        id=row.id,
        organization_id=row.organization_id,
        workflow_id=row.workflow_id,
        state=row.state,
        initiated_by=row.initiated_by,
    )


def _task_record(row: Task) -> TaskRecord:
    return TaskRecord(
        id=row.id,
        organization_id=row.organization_id,
        run_id=row.run_id,
        node_id=row.node_id,
        capability_id=row.capability_id,
        state=row.state,
        attempts=row.attempts,
        lease_id=row.lease_id,
        leased_by=row.leased_by,
        lease_expires_at=row.lease_expires_at,
        input=row.input,
        progress=row.progress,
        progress_updated_at=row.progress_updated_at,
        priority=row.priority,
        checkpoint=row.checkpoint,
        checkpoint_at=row.checkpoint_at,
    )


def _dead_letter_record(row: DeadLetter) -> DeadLetterRecord:
    return DeadLetterRecord(
        id=row.id,
        organization_id=row.organization_id,
        task_id=row.task_id,
        state=row.state,
        failure_reason=row.failure_reason,
        proposal_id=getattr(row, "proposal_id", None),
        diagnosis=getattr(row, "diagnosis", None),
        stack_trace=getattr(row, "stack_trace", None),
        last_checkpoint=getattr(row, "last_checkpoint", None),
        original_input=getattr(row, "original_input", None),
        dl_metadata=getattr(row, "dl_metadata", None) or {},
        attempts=getattr(row, "attempts", 0),
    )


class WorkflowService:
    """Use-case orchestration for the durable-execution spine (doc 04 §2.1)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- Workflow definitions ----------------------------------------------

    def create_workflow(
        self,
        context: OrgContext,
        *,
        name: str,
        created_by: uuid.UUID,
        spec: Mapping[str, Any] | None = None,
        version: int = 1,
    ) -> uuid.UUID:
        """Create a ``draft`` workflow definition, returning its id.

        A new definition always starts in ``draft`` (doc 03 §2.11); a spec is
        only required to be a valid DAG at publish time (INV-WF-1), so a draft
        may carry an incomplete or empty spec while it is being authored.
        """
        workflow_id = new_id()
        self._session.add(
            Workflow(
                id=workflow_id,
                organization_id=context.organization_id,
                name=name,
                version=version,
                spec={} if spec is None else dict(spec),
                state=WORKFLOW_STATE_DRAFT,
                created_by=created_by,
            )
        )
        self._session.flush()
        return workflow_id

    def publish_workflow(self, context: OrgContext, workflow_id: uuid.UUID) -> None:
        """Publish a workflow, rejecting any spec that is not a valid DAG.

        INV-WF-1: ``validate_workflow_dag`` runs over the stored spec first, so
        a cyclic or malformed graph raises (:class:`WorkflowCycleError` /
        :class:`WorkflowSpecError`) and the row is left untouched in ``draft``.
        Only a validated spec is promoted to ``published``.
        """
        row = self._session.scalar(
            select(Workflow).where(
                Workflow.organization_id == context.organization_id,
                Workflow.id == workflow_id,
            )
        )
        if row is None:
            raise WorkflowNotFoundError(workflow_id)

        validate_workflow_dag(row.spec)

        row.state = WORKFLOW_STATE_PUBLISHED
        self._session.flush()

    def get_workflow(
        self, context: OrgContext, workflow_id: uuid.UUID
    ) -> WorkflowRecord | None:
        """Fetch one workflow in this org, or ``None`` if it does not exist."""
        row = self._session.scalar(
            select(Workflow).where(
                Workflow.organization_id == context.organization_id,
                Workflow.id == workflow_id,
            )
        )
        return None if row is None else _workflow_record(row)

    # -- Runs --------------------------------------------------------------

    def start_run(
        self,
        context: OrgContext,
        workflow_id: uuid.UUID,
        *,
        initiated_by: uuid.UUID,
    ) -> uuid.UUID:
        """Queue a new run of an existing workflow, returning its id.

        The composite FK guarantees the workflow is in this org (INV-TABLE-1);
        an absent workflow is surfaced as a clean domain error rather than a raw
        ``IntegrityError``. Runs begin ``queued`` (doc 03 §4.4).
        """
        if self.get_workflow(context, workflow_id) is None:
            raise WorkflowNotFoundError(workflow_id)

        # M-1: quota enforcement — check concurrent run limit.
        from xiosync.services.quotas import QuotaService
        QuotaService(self._session).check_concurrent_runs(context.organization_id)

        run_id = new_id()
        self._session.add(
            WorkflowRun(
                id=run_id,
                organization_id=context.organization_id,
                workflow_id=workflow_id,
                state=WORKFLOW_RUN_STATE_QUEUED,
                initiated_by=initiated_by,
            )
        )
        self._session.flush()
        return run_id

    def get_run(self, context: OrgContext, run_id: uuid.UUID) -> WorkflowRunRecord | None:
        """Fetch one run in this org, or ``None`` if it does not exist."""
        row = self._session.scalar(
            select(WorkflowRun).where(
                WorkflowRun.organization_id == context.organization_id,
                WorkflowRun.id == run_id,
            )
        )
        return None if row is None else _run_record(row)

    # -- Tasks -------------------------------------------------------------

    def enqueue_task(
        self,
        context: OrgContext,
        run_id: uuid.UUID,
        *,
        node_id: str,
        capability_id: uuid.UUID,
        input: Mapping[str, Any] | None = None,
        priority: int = 5,
    ) -> uuid.UUID:
        """Enqueue a ``queued`` task for a run node, returning its id.

        A freshly enqueued task carries no lease (all lease fields null) and
        zero attempts; leasing is the control<->execution channel added in a
        later step (doc 04 §3.1 / INV-EXEC-1).

        Gap T-1: ``input`` is the task's input parameters, delivered to the
        worker at lease time.
        Gap X-3: ``priority`` (0–10, default 5) controls dispatch ordering.
        """
        # M-1: quota enforcement — check queued task limit before enqueueing.
        from xiosync.services.quotas import QuotaService
        QuotaService(self._session).check_queued_tasks(context.organization_id)

        task_id = new_id()
        self._session.add(
            Task(
                id=task_id,
                organization_id=context.organization_id,
                run_id=run_id,
                node_id=node_id,
                capability_id=capability_id,
                state=TASK_STATE_QUEUED,
                attempts=0,
                input=dict(input) if input is not None else None,
                priority=priority,
            )
        )
        self._session.flush()
        return task_id

    def get_task(self, context: OrgContext, task_id: uuid.UUID) -> TaskRecord | None:
        """Fetch one task in this org, or ``None`` if it does not exist."""
        row = self._session.scalar(
            select(Task).where(
                Task.organization_id == context.organization_id,
                Task.id == task_id,
            )
        )
        return None if row is None else _task_record(row)

    # -- Dead-letter queue -------------------------------------------------

    def dead_letter_task(
        self,
        context: OrgContext,
        task_id: uuid.UUID,
        *,
        failure_reason: str | None = None,
    ) -> uuid.UUID:
        """Route a failed task to the DLQ, returning the dead-letter id.

        INV-DLQ-1: the task moves to the terminal ``dead_letter`` state and a
        ``dead_letters`` row is created in the landing state ``open``. Nothing
        here resolves it — triage and any correction proposal are governed acts
        performed later (INV-DLQ-2/3), never a side effect of landing.
        """
        row = self._session.scalar(
            select(Task).where(
                Task.organization_id == context.organization_id,
                Task.id == task_id,
            )
        )
        if row is None:
            raise TaskNotFoundError(task_id)

        row.state = TASK_STATE_DEAD_LETTER

        dead_letter_id = new_id()
        self._session.add(
            DeadLetter(
                id=dead_letter_id,
                organization_id=context.organization_id,
                task_id=task_id,
                failure_reason=failure_reason,
                state=DEAD_LETTER_LANDING_STATE,
            )
        )
        self._session.flush()
        return dead_letter_id

    def get_dead_letter(
        self, context: OrgContext, dead_letter_id: uuid.UUID
    ) -> DeadLetterRecord | None:
        """Fetch one dead-letter record in this org, or ``None`` if absent."""
        row = self._session.scalar(
            select(DeadLetter).where(
                DeadLetter.organization_id == context.organization_id,
                DeadLetter.id == dead_letter_id,
            )
        )
        return None if row is None else _dead_letter_record(row)

    def propose_dlq_correction(
        self,
        context: OrgContext,
        dead_letter_id: uuid.UUID,
        *,
        diagnosis: Mapping[str, Any],
    ) -> uuid.UUID:
        """Attach a correction proposal to a dead-letter record (INV-DLQ-2).

        The governed correction flow: a proposal_id is generated and stored
        alongside the diagnosis on the dead-letter record, and the state
        advances from ``open`` to ``investigating``. No spec mutation or
        auto-resolution takes place here — governance requires a separate
        explicit ``resolve_dead_letter`` call with ``explicit_approval=True``
        (INV-DLQ-3).

        Raises :class:`DeadLetterNotFoundError` if the record is absent, or
        :class:`ValueError` if the record is not in the ``open`` state (it
        already has a proposal or is resolved).
        """
        row = self._session.scalar(
            select(DeadLetter)
            .where(
                DeadLetter.organization_id == context.organization_id,
                DeadLetter.id == dead_letter_id,
            )
            .with_for_update()
        )
        if row is None:
            raise DeadLetterNotFoundError(dead_letter_id)
        if not dead_letter_accepts_proposal(row.state):
            raise ValueError(
                f"dead_letter {dead_letter_id} is in state {row.state!r} "
                "and does not accept a new correction proposal"
            )

        proposal_id = new_id()
        row.proposal_id = proposal_id
        row.diagnosis = dict(diagnosis)
        row.state = DEAD_LETTER_STATE_INVESTIGATING
        self._session.flush()
        return proposal_id

    def resolve_dead_letter(
        self,
        context: OrgContext,
        dead_letter_id: uuid.UUID,
        *,
        explicit_approval: bool = False,
    ) -> None:
        """Resolve a dead-letter record, gated by explicit human/policy approval.

        INV-DLQ-3: ``explicit_approval`` must be ``True`` — auto-resolution is
        never permitted. The record must be in ``investigating`` state (a
        proposal must have been submitted first). The spec correction/promotion
        is a separate act (INV-DLQ-4); this method only closes the DLQ record.

        Raises :class:`DeadLetterNotFoundError` if absent, or
        :class:`ValueError` if the record is not approvable (wrong state or
        ``explicit_approval=False``).
        """
        row = self._session.scalar(
            select(DeadLetter)
            .where(
                DeadLetter.organization_id == context.organization_id,
                DeadLetter.id == dead_letter_id,
            )
            .with_for_update()
        )
        if row is None:
            raise DeadLetterNotFoundError(dead_letter_id)
        if not dead_letter_is_approvable(row.state, explicit_approval):
            raise ValueError(
                f"dead_letter {dead_letter_id} cannot be resolved: "
                f"state={row.state!r}, explicit_approval={explicit_approval}"
            )

        row.state = DEAD_LETTER_STATE_RESOLVED
        self._session.flush()

    # -- Lease protocol (INV-EXEC-1/2, doc 07 §1.1) ---------------------------

    _DEFAULT_LEASE_DURATION: timedelta = timedelta(minutes=5)

    def _locked_task(
        self,
        context: OrgContext,
        task_id: uuid.UUID,
    ) -> Task:
        """Fetch a task row with a row-level lock, raising if absent."""
        row = self._session.scalar(
            select(Task)
            .where(
                Task.organization_id == context.organization_id,
                Task.id == task_id,
            )
            .with_for_update()
        )
        if row is None:
            raise TaskNotFoundError(task_id)
        return row

    def lease_task(
        self,
        context: OrgContext,
        task_id: uuid.UUID,
        *,
        leased_by: uuid.UUID,    # worker actor ID (FK → actors.id)
        duration: timedelta | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        """Atomically acquire a lease on a ``queued`` task (INV-EXEC-1).

        A row-level lock prevents double-leasing under concurrent workers.
        The task must be in the ``queued`` state; any other state raises
        :class:`UnleaseableError`. On success the task transitions to
        ``leased``, the attempt counter increments, and a new lease_id,
        leased_by, and lease_expires_at are recorded.
        """
        effective_now = now if now is not None else datetime.now(UTC)
        effective_duration = duration if duration is not None else self._DEFAULT_LEASE_DURATION

        row = self._locked_task(context, task_id)
        if not task_is_leaseable(row.state):
            raise UnleaseableError(task_id, row.state)

        row.lease_id = new_id()
        row.leased_by = leased_by
        row.lease_expires_at = effective_now + effective_duration
        row.state = TASK_STATE_LEASED
        row.attempts = (row.attempts or 0) + 1
        self._session.flush()
        return _task_record(row)

    def heartbeat_task(
        self,
        context: OrgContext,
        task_id: uuid.UUID,
        *,
        lease_id: uuid.UUID,
        duration: timedelta | None = None,
        progress: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        """Extend an active lease by *duration* to prevent expiry mid-work.

        The caller must present the same ``lease_id`` that was returned by
        :meth:`lease_task`. An expired or mismatched lease raises
        :class:`InactiveLeaseError`.

        Gap T-4: when ``progress`` is provided, the task's progress snapshot
        and ``progress_updated_at`` timestamp are updated atomically with the
        lease extension.
        """
        effective_now = now if now is not None else datetime.now(UTC)
        effective_duration = duration if duration is not None else self._DEFAULT_LEASE_DURATION

        row = self._locked_task(context, task_id)
        if row.lease_id != lease_id:
            raise InactiveLeaseError(task_id, "lease_id mismatch")
        if lease_has_expired(row.state, row.lease_expires_at, effective_now):
            raise InactiveLeaseError(task_id, "lease has expired")

        row.lease_expires_at = effective_now + effective_duration
        if progress is not None:
            row.progress = dict(progress)
            row.progress_updated_at = effective_now
        self._session.flush()
        return _task_record(row)

    def complete_task(
        self,
        context: OrgContext,
        task_id: uuid.UUID,
        *,
        lease_id: uuid.UUID,
        result: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> CompletionOutcome:
        """Mark a leased task as ``completed`` — idempotent on duplicate calls.

        INV-EXEC-2: a task may only be completed once. If the task is already
        in the ``completed`` state the method returns a :class:`CompletionOutcome`
        with ``duplicate=True`` instead of raising; all other concurrent
        completion attempts are protected by the row-level lock acquired inside
        :meth:`_locked_task`.

        Raises :class:`TaskNotFoundError`, :class:`InactiveLeaseError` (wrong
        lease_id or expired lease), or :class:`NonCompletableError` (task is in
        a non-completable non-completed state).
        """
        effective_now = now if now is not None else datetime.now(UTC)
        row = self._locked_task(context, task_id)

        # Idempotency: already completed → return duplicate signal, no mutation.
        if task_is_completed(row.state):
            return CompletionOutcome(
                task_id=task_id,
                state=row.state,
                result=getattr(row, "result", None),
                duplicate=True,
            )

        if not task_is_completable(row.state):
            raise NonCompletableError(task_id, row.state)
        if row.lease_id != lease_id:
            raise InactiveLeaseError(task_id, "lease_id mismatch on completion")
        if lease_has_expired(row.state, row.lease_expires_at, effective_now):
            raise InactiveLeaseError(task_id, "lease expired before completion")

        row.state = TASK_STATE_COMPLETED
        row.lease_expires_at = None
        if result is not None and hasattr(row, "result"):
            row.result = dict(result)
        self._session.flush()

        # M-3: metering instrumentation — record task_runs metric.
        try:
            from xiosync.services.metering import MeteringService
            MeteringService(self._session).record_usage(
                context, metric_type="task_runs",
            )
        except Exception:
            pass  # Metering is best-effort.

        # T-2: advance DAG — enqueue downstream tasks whose deps are now met.
        outcome = CompletionOutcome(
            task_id=task_id,
            state=TASK_STATE_COMPLETED,
            result=dict(result) if result is not None else None,
            duplicate=False,
        )
        self.advance_dag(context, row.run_id, completed_task_node_id=row.node_id)
        return outcome

    def advance_dag(
        self,
        context: OrgContext,
        run_id: uuid.UUID,
        *,
        completed_task_node_id: str | None = None,
    ) -> list[uuid.UUID]:
        """Advance DAG execution after a task completes (Gap T-2).

        Finds downstream nodes whose upstream dependencies are all completed,
        resolves their ``input_from`` declarations against upstream results,
        and auto-enqueues them. Returns IDs of newly enqueued tasks.

        This is the runtime counterpart of ``validate_data_flow()`` in the
        domain layer.
        """
        from xiosync.domain.workflows import resolve_node_inputs

        run_row = self._session.scalar(
            select(WorkflowRun).where(
                WorkflowRun.organization_id == context.organization_id,
                WorkflowRun.id == run_id,
            )
        )
        if run_row is None:
            return []

        wf_row = self._session.scalar(
            select(Workflow).where(
                Workflow.organization_id == context.organization_id,
                Workflow.id == run_row.workflow_id,
            )
        )
        if wf_row is None or not wf_row.spec:
            return []

        spec = wf_row.spec
        nodes = spec.get("nodes", [])
        edges = spec.get("edges", [])

        # Build adjacency: for each node, what are its upstream deps?
        # edges = [{"from": "A", "to": "B"}, ...] means A must complete before B
        upstream_deps: dict[str, set[str]] = {
            n["id"]: set() for n in nodes if isinstance(n, dict) and "id" in n
        }
        for edge in edges:
            if isinstance(edge, dict):
                src = edge.get("from")
                tgt = edge.get("to")
                if src and tgt and tgt in upstream_deps:
                    upstream_deps[tgt].add(src)

        # Get all tasks for this run to know which nodes are completed.
        run_tasks = self._session.scalars(
            select(Task).where(
                Task.organization_id == context.organization_id,
                Task.run_id == run_id,
            )
        ).all()

        completed_nodes: dict[str, Any] = {}
        enqueued_nodes: set[str] = set()
        for t in run_tasks:
            if t.state == TASK_STATE_COMPLETED:
                completed_nodes[t.node_id] = getattr(t, "result", None)
            enqueued_nodes.add(t.node_id)

        # Find downstream candidates: all deps completed, not yet enqueued.
        new_task_ids: list[uuid.UUID] = []
        for node in nodes:
            if not isinstance(node, dict) or "id" not in node:
                continue
            node_id = node["id"]
            if node_id in enqueued_nodes:
                continue  # Already has a task
            deps = upstream_deps.get(node_id, set())
            if not deps:
                continue  # Root node, should already be enqueued
            if not deps.issubset(completed_nodes.keys()):
                continue  # Not all deps completed yet

            # All deps met — resolve inputs and enqueue.
            input_from = node.get("input_from")
            resolved_input: dict[str, Any] | None = None
            if input_from is not None:
                resolved_input = resolve_node_inputs(input_from, completed_nodes)

            # Use the node's capability_id (must be in spec).
            cap_id_raw = node.get("capability_id")
            if cap_id_raw is None:
                continue  # Can't enqueue without a capability
            try:
                cap_id = uuid.UUID(str(cap_id_raw))
            except (ValueError, TypeError):
                continue

            priority = node.get("priority", 5)
            task_id = self.enqueue_task(
                context,
                run_id,
                node_id=node_id,
                capability_id=cap_id,
                input=resolved_input,
                priority=priority,
            )
            new_task_ids.append(task_id)

        return new_task_ids

    def expire_leases(
        self,
        context: OrgContext,
        *,
        now: datetime | None = None,
    ) -> Sequence[uuid.UUID]:
        """Reclaim all tasks whose leases have expired, returning their ids.

        Expired ``leased`` tasks are returned to the ``queued`` state so they
        can be re-leased by a fresh worker. The lease fields are cleared. This
        method is intended for a periodic reconciler (doc 07 §1.1) and is safe
        to call concurrently — each row is updated atomically within the
        caller's transaction.
        """
        effective_now = now if now is not None else datetime.now(UTC)

        rows = list(
            self._session.scalars(
                select(Task).where(
                    Task.organization_id == context.organization_id,
                    Task.state == TASK_STATE_LEASED,
                )
            )
        )

        reclaimed: list[uuid.UUID] = []
        for row in rows:
            if lease_has_expired(row.state, row.lease_expires_at, effective_now):
                row.state = TASK_STATE_QUEUED
                row.lease_id = None
                row.leased_by = None
                row.lease_expires_at = None
                reclaimed.append(row.id)

        if reclaimed:
            self._session.flush()
        return reclaimed

    # -- Gap W-2: Task Discovery / Atomic Claim --------------------------

    def claim_next_task(
        self,
        context: OrgContext,
        *,
        worker_id: uuid.UUID,
        capabilities: list[str],
        duration: int,
        now: datetime | None = None,
    ) -> TaskRecord | None:
        """Atomically find and lease the highest-priority queued task matching
        the worker's capabilities.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` to prevent contention
        between concurrent workers. Returns ``None`` when no matching task
        is available.

        Gap W-2: This is the primary task discovery mechanism for workers.
        """
        from xiosync.domain.capabilities import capability_matches
        from xiosync.persistence.models.authorization import Capability

        effective_now = now if now is not None else datetime.now(UTC)

        # Find queued tasks ordered by priority DESC, created_at ASC.
        rows = list(
            self._session.scalars(
                select(Task)
                .where(
                    Task.organization_id == context.organization_id,
                    Task.state == TASK_STATE_QUEUED,
                )
                .order_by(Task.priority.desc(), Task.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(50)  # scan batch size
            )
        )

        for row in rows:
            # Look up the capability name for this task.
            cap_row = self._session.scalar(
                select(Capability.name).where(
                    Capability.organization_id == context.organization_id,
                    Capability.id == row.capability_id,
                )
            )
            cap_name = cap_row if cap_row else str(row.capability_id)

            if not capability_matches(capabilities, cap_name):
                continue

            # Match found — lease it.
            lease_id = new_id()
            row.state = TASK_STATE_LEASED
            row.lease_id = lease_id
            row.leased_by = worker_id
            row.lease_expires_at = effective_now + timedelta(seconds=duration)
            row.attempts += 1
            self._session.flush()
            return _task_record(row)

        return None

    # -- Gap W-3: Task Checkpointing -------------------------------------

    def checkpoint_task(
        self,
        context: OrgContext,
        *,
        task_id: uuid.UUID,
        lease_id: uuid.UUID,
        checkpoint: dict[str, Any],
        now: datetime | None = None,
    ) -> TaskRecord:
        """Write checkpoint data for an actively-leased task.

        The checkpoint is atomically written with a timestamp. On task retry
        (after lease expiry or worker crash), the checkpoint is included in
        the ``LeaseResponse`` so the new worker can resume from it.

        Gap W-3: enables stateful, long-running tasks to survive crashes.
        """
        effective_now = now if now is not None else datetime.now(UTC)

        row = self._session.scalar(
            select(Task)
            .where(
                Task.organization_id == context.organization_id,
                Task.id == task_id,
            )
            .with_for_update()
        )
        if row is None:
            raise TaskNotFoundError(task_id)

        # Validate active lease.
        if row.state != TASK_STATE_LEASED:
            raise InactiveLeaseError(task_id, f"task state is {row.state!r}, not leased")
        if row.lease_id != lease_id:
            raise InactiveLeaseError(task_id, "lease_id mismatch")
        if lease_has_expired(row.state, row.lease_expires_at, effective_now):
            raise InactiveLeaseError(task_id, "lease has expired")

        row.checkpoint = dict(checkpoint)
        row.checkpoint_at = effective_now
        self._session.flush()
        return _task_record(row)

