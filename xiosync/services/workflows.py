"""Workflow service — lifecycle, task lease protocol, and DLQ governance.

This module provides:
- WorkflowService: manages workflow CRUD, run lifecycle, task leasing, and DLQ.
- Typed result dataclasses: TaskRecord, DeadLetterRecord, CompletionOutcome.
- Domain exceptions for every failure path.

The integration tests in tests/integration/test_workflows.py exercise this
service end-to-end against a real PostgreSQL schema.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TaskNotFoundError(LookupError):
    """No task with the given id exists in this org's scope."""

    def __init__(self, task_id: uuid.UUID) -> None:
        self.task_id = task_id
        super().__init__(f"task not found: {task_id}")


class UnleaseableError(RuntimeError):
    """The task cannot be leased because it is not in the ``queued`` state."""

    def __init__(self, task_id: uuid.UUID, state: str) -> None:
        self.task_id = task_id
        self.state = state
        super().__init__(f"task {task_id} is not leaseable (state={state!r})")


class InactiveLeaseError(RuntimeError):
    """The supplied lease_id does not match the active lease."""

    def __init__(self, task_id: uuid.UUID, reason: str = "lease mismatch") -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"inactive lease for task {task_id}: {reason}")


class NonCompletableError(RuntimeError):
    """The task cannot be completed because it is not in the ``leased`` state."""

    def __init__(self, task_id: uuid.UUID, state: str) -> None:
        self.task_id = task_id
        self.state = state
        super().__init__(f"task {task_id} is not completable (state={state!r})")


class DeadLetterNotFoundError(LookupError):
    """No dead-letter record with the given id exists."""

    def __init__(self, dead_letter_id: uuid.UUID) -> None:
        self.dead_letter_id = dead_letter_id
        super().__init__(f"dead letter not found: {dead_letter_id}")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TaskRecord:
    """Snapshot of a workflow task row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    run_id: uuid.UUID
    node_id: str
    capability_id: uuid.UUID
    state: str
    attempts: int
    lease_id: uuid.UUID | None
    leased_by: uuid.UUID | None
    lease_expires_at: datetime | None
    input: dict[str, Any] | None
    progress: dict[str, Any] | None
    progress_updated_at: datetime | None
    priority: int = 5
    checkpoint: dict[str, Any] | None = None
    checkpoint_at: datetime | None = None


@dataclass
class DeadLetterRecord:
    """Snapshot of a dead_letters row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    task_id: uuid.UUID
    state: str
    failure_reason: str
    proposal_id: uuid.UUID | None = None
    diagnosis: dict[str, Any] | None = None
    stack_trace: str | None = None
    last_checkpoint: dict[str, Any] | None = None
    original_input: dict[str, Any] | None = None
    dl_metadata: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1


@dataclass
class CompletionOutcome:
    """Result returned by WorkflowService.complete_task."""

    task_id: uuid.UUID
    state: str
    result: Any
    duplicate: bool


# ---------------------------------------------------------------------------
# WorkflowService
# ---------------------------------------------------------------------------


class WorkflowService:
    """Service for managing workflows, runs, tasks, and the DLQ.

    All methods operate within a caller-supplied SQLAlchemy session that has
    been configured with row-level security for the caller's org via
    ``org_scoped_session``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Workflow CRUD
    # ------------------------------------------------------------------

    def create_workflow(
        self,
        context: Any,
        *,
        name: str,
        created_by: uuid.UUID,
        spec: dict[str, Any],
    ) -> uuid.UUID:
        """Create a workflow in ``draft`` state and return its id."""
        from xiosync.platform.ids import new_id

        workflow_id = new_id()
        import json as _json

        self._session.execute(
            text(
                "INSERT INTO workflows (id, organization_id, name, spec, state, created_by) "
                "VALUES (:id, :org, :name, cast(:spec as jsonb), 'draft', :created_by)"
            ),
            {
                "id": workflow_id,
                "org": context.organization_id,
                "name": name,
                "spec": _json.dumps(spec),
                "created_by": created_by,
            },
        )
        return workflow_id

    def publish_workflow(self, context: Any, workflow_id: uuid.UUID) -> None:
        """Validate and publish a draft workflow.

        Raises WorkflowCycleError or WorkflowSpecError on invalid specs.
        """
        from xiosync.domain.workflows import validate_workflow_dag

        row = self._session.execute(
            text("SELECT spec FROM workflows WHERE id = :id"),
            {"id": workflow_id},
        ).one()

        import json as _json

        spec = _json.loads(row.spec) if isinstance(row.spec, str) else dict(row.spec)
        validate_workflow_dag(spec)

        self._session.execute(
            text("UPDATE workflows SET state = 'published' WHERE id = :id"),
            {"id": workflow_id},
        )

    def start_run(
        self,
        context: Any,
        workflow_id: uuid.UUID,
        *,
        initiated_by: uuid.UUID,
    ) -> uuid.UUID:
        """Create a workflow run in ``running`` state and return its id."""
        from xiosync.platform.ids import new_id

        run_id = new_id()
        self._session.execute(
            text(
                "INSERT INTO workflow_runs (id, organization_id, workflow_id, state, initiated_by) "
                "VALUES (:id, :org, :wf, 'running', :by)"
            ),
            {
                "id": run_id,
                "org": context.organization_id,
                "wf": workflow_id,
                "by": initiated_by,
            },
        )
        return run_id

    def enqueue_task(
        self,
        context: Any,
        run_id: uuid.UUID,
        *,
        node_id: str,
        capability_id: uuid.UUID,
        priority: int = 5,
        input_data: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Enqueue a task for execution and return its id."""
        from xiosync.platform.ids import new_id
        import json as _json

        task_id = new_id()
        self._session.execute(
            text(
                "INSERT INTO workflow_tasks "
                "(id, organization_id, run_id, node_id, capability_id, state, priority, input, attempts) "
                "VALUES (:id, :org, :run, :node, :cap, 'queued', :pri, cast(:inp as jsonb), 0)"
            ),
            {
                "id": task_id,
                "org": context.organization_id,
                "run": run_id,
                "node": node_id,
                "cap": capability_id,
                "pri": priority,
                "inp": _json.dumps(input_data or {}),
            },
        )
        return task_id

    # ------------------------------------------------------------------
    # Task lease protocol
    # ------------------------------------------------------------------

    def get_task(self, context: Any, task_id: uuid.UUID) -> TaskRecord | None:
        """Fetch a TaskRecord by id, returning None if not found."""
        row = self._session.execute(
            text("SELECT * FROM workflow_tasks WHERE id = :id"),
            {"id": task_id},
        ).mappings().first()
        if row is None:
            return None
        return self._row_to_task(row)

    def lease_task(
        self,
        context: Any,
        task_id: uuid.UUID,
        *,
        leased_by: uuid.UUID,
        duration: timedelta,
        now: datetime,
    ) -> TaskRecord:
        """Atomically transition a ``queued`` task to ``leased``.

        Raises
        ------
        TaskNotFoundError   – task does not exist
        UnleaseableError    – task is not in ``queued`` state
        """
        row = self._session.execute(
            text("SELECT * FROM workflow_tasks WHERE id = :id"),
            {"id": task_id},
        ).mappings().first()

        if row is None:
            raise TaskNotFoundError(task_id)
        if row["state"] != "queued":
            raise UnleaseableError(task_id, row["state"])

        import uuid as _uuid

        lease_id = _uuid.uuid4()
        expires_at = now + duration

        self._session.execute(
            text(
                "UPDATE workflow_tasks SET state='leased', lease_id=:lease, "
                "leased_by=:by, lease_expires_at=:exp, attempts=attempts+1 "
                "WHERE id=:id"
            ),
            {"lease": lease_id, "by": leased_by, "exp": expires_at, "id": task_id},
        )

        updated = self._session.execute(
            text("SELECT * FROM workflow_tasks WHERE id = :id"),
            {"id": task_id},
        ).mappings().first()
        assert updated is not None
        return self._row_to_task(updated)

    def heartbeat_task(
        self,
        context: Any,
        task_id: uuid.UUID,
        *,
        lease_id: uuid.UUID,
        duration: timedelta,
        now: datetime,
    ) -> TaskRecord:
        """Extend the lease expiry for an active task.

        Raises InactiveLeaseError if the lease_id does not match.
        """
        row = self._session.execute(
            text("SELECT * FROM workflow_tasks WHERE id = :id"),
            {"id": task_id},
        ).mappings().first()

        if row is None or row["lease_id"] != lease_id:
            raise InactiveLeaseError(task_id, "lease_id mismatch")

        expires_at = now + duration
        self._session.execute(
            text("UPDATE workflow_tasks SET lease_expires_at=:exp WHERE id=:id"),
            {"exp": expires_at, "id": task_id},
        )

        updated = self._session.execute(
            text("SELECT * FROM workflow_tasks WHERE id = :id"),
            {"id": task_id},
        ).mappings().first()
        assert updated is not None
        return self._row_to_task(updated)

    def complete_task(
        self,
        context: Any,
        task_id: uuid.UUID,
        *,
        lease_id: uuid.UUID,
        result: Any = None,
        now: datetime | None = None,
    ) -> CompletionOutcome:
        """Mark a leased task as completed.

        Idempotent: if the task is already ``completed``, returns
        ``CompletionOutcome(duplicate=True)``.

        Raises InactiveLeaseError if the lease_id does not match.
        Raises NonCompletableError if the task is in an unexpected state.
        """
        row = self._session.execute(
            text("SELECT * FROM workflow_tasks WHERE id = :id"),
            {"id": task_id},
        ).mappings().first()

        if row is None:
            raise TaskNotFoundError(task_id)

        if row["state"] == "completed":
            return CompletionOutcome(
                task_id=task_id, state="completed", result=None, duplicate=True
            )

        if row["state"] != "leased":
            raise NonCompletableError(task_id, row["state"])

        if row["lease_id"] != lease_id:
            raise InactiveLeaseError(task_id, "lease_id mismatch")

        self._session.execute(
            text(
                "UPDATE workflow_tasks SET state='completed', lease_id=NULL, "
                "leased_by=NULL, lease_expires_at=NULL WHERE id=:id"
            ),
            {"id": task_id},
        )
        return CompletionOutcome(
            task_id=task_id, state="completed", result=result, duplicate=False
        )

    def expire_leases(self, context: Any, *, now: datetime) -> list[uuid.UUID]:
        """Reclaim tasks whose lease has expired back to ``queued``.

        Returns the list of reclaimed task ids.
        """
        rows = self._session.execute(
            text(
                "SELECT id FROM workflow_tasks "
                "WHERE state='leased' AND lease_expires_at < :now "
                "AND organization_id = :org"
            ),
            {"now": now, "org": context.organization_id},
        ).fetchall()

        task_ids = [row[0] for row in rows]
        if task_ids:
            self._session.execute(
                text(
                    "UPDATE workflow_tasks SET state='queued', lease_id=NULL, "
                    "leased_by=NULL, lease_expires_at=NULL "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": task_ids},
            )
        return task_ids

    def dead_letter_task(
        self,
        context: Any,
        task_id: uuid.UUID,
        *,
        failure_reason: str,
        stack_trace: str | None = None,
        last_checkpoint: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Move a task to the ``dead_letter`` state and create a DLQ record."""
        from xiosync.platform.ids import new_id
        import json as _json

        task_row = self._session.execute(
            text("SELECT * FROM workflow_tasks WHERE id = :id"),
            {"id": task_id},
        ).mappings().first()
        if task_row is None:
            raise TaskNotFoundError(task_id)

        self._session.execute(
            text("UPDATE workflow_tasks SET state='dead_letter' WHERE id=:id"),
            {"id": task_id},
        )

        dl_id = new_id()
        self._session.execute(
            text(
                "INSERT INTO dead_letters "
                "(id, organization_id, task_id, state, failure_reason, stack_trace, "
                "last_checkpoint, original_input, attempts) "
                "VALUES (:id, :org, :task, 'open', :reason, :trace, "
                "cast(:chk as jsonb), cast(:inp as jsonb), :att)"
            ),
            {
                "id": dl_id,
                "org": context.organization_id,
                "task": task_id,
                "reason": failure_reason,
                "trace": stack_trace,
                "chk": _json.dumps(last_checkpoint or {}),
                "inp": _json.dumps(
                    _json.loads(task_row["input"]) if task_row.get("input") else {}
                ),
                "att": task_row.get("attempts", 1),
            },
        )
        return dl_id

    # ------------------------------------------------------------------
    # DLQ governance
    # ------------------------------------------------------------------

    def get_dead_letter(
        self, context: Any, dead_letter_id: uuid.UUID
    ) -> DeadLetterRecord | None:
        """Fetch a DeadLetterRecord by id."""
        row = self._session.execute(
            text("SELECT * FROM dead_letters WHERE id = :id"),
            {"id": dead_letter_id},
        ).mappings().first()
        if row is None:
            return None
        return self._row_to_dl(row)

    def propose_dlq_correction(
        self,
        context: Any,
        dead_letter_id: uuid.UUID,
        *,
        diagnosis: dict[str, Any],
    ) -> uuid.UUID:
        """Advance an ``open`` DLQ record to ``investigating``.

        Raises ValueError if the record is not in ``open`` state.
        Raises DeadLetterNotFoundError if not found.
        """
        from xiosync.platform.ids import new_id
        import json as _json

        row = self._session.execute(
            text("SELECT * FROM dead_letters WHERE id = :id"),
            {"id": dead_letter_id},
        ).mappings().first()
        if row is None:
            raise DeadLetterNotFoundError(dead_letter_id)
        if row["state"] != "open":
            raise ValueError(
                f"dead_letter is in state {row['state']!r} and does not accept a new proposal"
            )

        proposal_id = new_id()
        self._session.execute(
            text(
                "UPDATE dead_letters SET state='investigating', "
                "proposal_id=:pid, diagnosis=cast(:diag as jsonb) WHERE id=:id"
            ),
            {
                "pid": proposal_id,
                "diag": _json.dumps(diagnosis),
                "id": dead_letter_id,
            },
        )
        return proposal_id

    def resolve_dead_letter(
        self,
        context: Any,
        dead_letter_id: uuid.UUID,
        *,
        explicit_approval: bool,
    ) -> None:
        """Resolve an ``investigating`` DLQ record.

        Raises ValueError if ``explicit_approval`` is False.
        Raises ValueError if the record is not in ``investigating`` state.
        Raises DeadLetterNotFoundError if not found.
        """
        if not explicit_approval:
            raise ValueError("resolve_dead_letter requires explicit_approval=True")

        row = self._session.execute(
            text("SELECT * FROM dead_letters WHERE id = :id"),
            {"id": dead_letter_id},
        ).mappings().first()
        if row is None:
            raise DeadLetterNotFoundError(dead_letter_id)
        if row["state"] != "investigating":
            raise ValueError(
                f"dead_letter cannot be resolved: state={row['state']!r}"
            )

        self._session.execute(
            text("UPDATE dead_letters SET state='resolved' WHERE id=:id"),
            {"id": dead_letter_id},
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_task(row: Any) -> TaskRecord:
        import json as _json

        def _uuid(v: Any) -> uuid.UUID | None:
            if v is None:
                return None
            return uuid.UUID(str(v)) if not isinstance(v, uuid.UUID) else v

        def _dict(v: Any) -> dict | None:
            if v is None:
                return None
            if isinstance(v, str):
                return _json.loads(v)
            return dict(v)

        return TaskRecord(
            id=_uuid(row["id"]),  # type: ignore[arg-type]
            organization_id=_uuid(row["organization_id"]),  # type: ignore[arg-type]
            run_id=_uuid(row["run_id"]),  # type: ignore[arg-type]
            node_id=str(row["node_id"]),
            capability_id=_uuid(row["capability_id"]),  # type: ignore[arg-type]
            state=str(row["state"]),
            attempts=int(row["attempts"]),
            lease_id=_uuid(row.get("lease_id")),
            leased_by=_uuid(row.get("leased_by")),
            lease_expires_at=row.get("lease_expires_at"),
            input=_dict(row.get("input")),
            progress=_dict(row.get("progress")),
            progress_updated_at=row.get("progress_updated_at"),
            priority=int(row.get("priority", 5)),
            checkpoint=_dict(row.get("checkpoint")),
            checkpoint_at=row.get("checkpoint_at"),
        )

    @staticmethod
    def _row_to_dl(row: Any) -> DeadLetterRecord:
        import json as _json

        def _uuid(v: Any) -> uuid.UUID | None:
            if v is None:
                return None
            return uuid.UUID(str(v)) if not isinstance(v, uuid.UUID) else v

        def _dict(v: Any) -> dict | None:
            if v is None:
                return None
            if isinstance(v, str):
                return _json.loads(v)
            return dict(v)

        return DeadLetterRecord(
            id=_uuid(row["id"]),  # type: ignore[arg-type]
            organization_id=_uuid(row["organization_id"]),  # type: ignore[arg-type]
            task_id=_uuid(row["task_id"]),  # type: ignore[arg-type]
            state=str(row["state"]),
            failure_reason=str(row["failure_reason"]),
            proposal_id=_uuid(row.get("proposal_id")),
            diagnosis=_dict(row.get("diagnosis")),
            stack_trace=row.get("stack_trace"),
            last_checkpoint=_dict(row.get("last_checkpoint")),
            original_input=_dict(row.get("original_input")),
            dl_metadata=_dict(row.get("dl_metadata")) or {},
            attempts=int(row.get("attempts", 1)),
        )
