"""Workflow trigger management (Gap R-5).

``TriggerService`` manages cron, event-driven, and webhook triggers for
automated workflow execution. Trigger evaluation (the actual polling/listening
loop) is configurable — it can run in-process or as a separate worker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.domain.triggers import (
    validate_cron_expression,
    validate_trigger_state,
    validate_trigger_type,
    next_cron_fire,
)
from xiosync.persistence.models.triggers import WorkflowTrigger
from xiosync.platform.ids import new_id

__all__ = [
    "TriggerNotFoundError",
    "TriggerRecord",
    "TriggerService",
]


@dataclass(frozen=True, slots=True)
class TriggerRecord:
    """Frozen snapshot of a ``workflow_triggers`` row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    workflow_id: uuid.UUID
    trigger_type: str
    config: dict[str, Any]
    state: str
    last_fired_at: datetime | None
    next_fire_at: datetime | None
    created_at: datetime
    created_by: uuid.UUID


class TriggerNotFoundError(ValueError):
    """Raised when the requested trigger does not exist."""


def _record(row: WorkflowTrigger) -> TriggerRecord:
    return TriggerRecord(
        id=row.id,
        organization_id=row.organization_id,
        workflow_id=row.workflow_id,
        trigger_type=row.trigger_type,
        config=dict(row.config),
        state=row.state,
        last_fired_at=row.last_fired_at,
        next_fire_at=row.next_fire_at,
        created_at=row.created_at,
        created_by=row.created_by,
    )


class TriggerService:
    """Use cases for workflow triggers (Gap R-5)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_trigger(
        self,
        context: OrgContext,
        *,
        workflow_id: uuid.UUID,
        trigger_type: str,
        config: dict[str, Any],
        created_by: uuid.UUID,
    ) -> TriggerRecord:
        """Create a new workflow trigger."""
        validate_trigger_type(trigger_type)

        # Validate cron config if applicable.
        if trigger_type == "cron":
            cron_expr = config.get("cron")
            if not isinstance(cron_expr, str):
                raise ValueError("cron trigger config must contain a 'cron' string")
            validate_cron_expression(cron_expr)

        trigger_id = new_id()
        now = datetime.now(timezone.utc)

        # Pre-compute next fire time for cron triggers.
        fire_at = None
        if trigger_type == "cron":
            fire_at = next_cron_fire(config["cron"], now)

        row = WorkflowTrigger(
            id=trigger_id,
            organization_id=context.organization_id,
            workflow_id=workflow_id,
            trigger_type=trigger_type,
            config=config,
            created_by=created_by,
            next_fire_at=fire_at,
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def get_trigger(
        self,
        context: OrgContext,
        trigger_id: uuid.UUID,
    ) -> TriggerRecord:
        """Fetch one trigger, or raise ``TriggerNotFoundError``."""
        row = self._session.scalar(
            select(WorkflowTrigger).where(
                WorkflowTrigger.organization_id == context.organization_id,
                WorkflowTrigger.id == trigger_id,
            )
        )
        if row is None:
            raise TriggerNotFoundError(
                f"trigger {trigger_id} not found in org {context.organization_id}"
            )
        return _record(row)

    def list_triggers(
        self,
        context: OrgContext,
        *,
        state: str | None = None,
        trigger_type: str | None = None,
        workflow_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[TriggerRecord]:
        """List triggers for this org."""
        stmt = (
            select(WorkflowTrigger)
            .where(WorkflowTrigger.organization_id == context.organization_id)
            .order_by(WorkflowTrigger.created_at.desc())
            .limit(limit)
        )
        if state is not None:
            stmt = stmt.where(WorkflowTrigger.state == state)
        if trigger_type is not None:
            stmt = stmt.where(WorkflowTrigger.trigger_type == trigger_type)
        if workflow_id is not None:
            stmt = stmt.where(WorkflowTrigger.workflow_id == workflow_id)
        return [_record(row) for row in self._session.scalars(stmt).all()]

    def pause_trigger(
        self,
        context: OrgContext,
        trigger_id: uuid.UUID,
    ) -> TriggerRecord:
        """Pause a trigger."""
        row = self._session.scalar(
            select(WorkflowTrigger).where(
                WorkflowTrigger.organization_id == context.organization_id,
                WorkflowTrigger.id == trigger_id,
            )
        )
        if row is None:
            raise TriggerNotFoundError(
                f"trigger {trigger_id} not found in org {context.organization_id}"
            )
        row.state = "paused"
        self._session.flush()
        return _record(row)

    def resume_trigger(
        self,
        context: OrgContext,
        trigger_id: uuid.UUID,
    ) -> TriggerRecord:
        """Resume a paused trigger."""
        row = self._session.scalar(
            select(WorkflowTrigger).where(
                WorkflowTrigger.organization_id == context.organization_id,
                WorkflowTrigger.id == trigger_id,
            )
        )
        if row is None:
            raise TriggerNotFoundError(
                f"trigger {trigger_id} not found in org {context.organization_id}"
            )
        row.state = "active"
        # Recompute next fire time for cron triggers.
        if row.trigger_type == "cron":
            cron_expr = row.config.get("cron")
            if isinstance(cron_expr, str):
                row.next_fire_at = next_cron_fire(cron_expr, datetime.now(timezone.utc))
        self._session.flush()
        return _record(row)

    def get_due_cron_triggers(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[TriggerRecord]:
        """Find cron triggers whose next_fire_at <= now."""
        ts = now or datetime.now(timezone.utc)
        stmt = (
            select(WorkflowTrigger)
            .where(
                WorkflowTrigger.trigger_type == "cron",
                WorkflowTrigger.state == "active",
                WorkflowTrigger.next_fire_at <= ts,
            )
            .order_by(WorkflowTrigger.next_fire_at)
            .limit(limit)
        )
        return [_record(row) for row in self._session.scalars(stmt).all()]

    def mark_fired(
        self,
        context: OrgContext,
        trigger_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> TriggerRecord:
        """Mark a trigger as fired and advance its next fire time."""
        ts = now or datetime.now(timezone.utc)
        row = self._session.scalar(
            select(WorkflowTrigger).where(
                WorkflowTrigger.organization_id == context.organization_id,
                WorkflowTrigger.id == trigger_id,
            )
        )
        if row is None:
            raise TriggerNotFoundError(
                f"trigger {trigger_id} not found in org {context.organization_id}"
            )
        row.last_fired_at = ts
        if row.trigger_type == "cron":
            cron_expr = row.config.get("cron")
            if isinstance(cron_expr, str):
                row.next_fire_at = next_cron_fire(cron_expr, ts)
        self._session.flush()
        return _record(row)

    def evaluate_event_triggers(
        self,
        context: OrgContext,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
    ) -> list[TriggerRecord]:
        """Find active event triggers matching the given event type."""
        stmt = (
            select(WorkflowTrigger)
            .where(
                WorkflowTrigger.organization_id == context.organization_id,
                WorkflowTrigger.trigger_type == "event",
                WorkflowTrigger.state == "active",
            )
        )
        rows = self._session.scalars(stmt).all()
        matching = []
        for row in rows:
            cfg_event_type = row.config.get("event_type")
            if cfg_event_type == event_type or cfg_event_type == "*":
                matching.append(_record(row))
        return matching
