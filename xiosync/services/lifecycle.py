"""Lifecycle use cases — legal transitions that always leave provenance (doc 03 §4).

``LifecycleService`` performs an actor lifecycle transition as one atomic unit:

1. Read the actor's current state inside the tenant scope.
2. Validate ``from_state -> to_state`` against the pure actor state machine
   (``domain/lifecycle``); an illegal transition raises before any write, so it
   produces no state change (INV-LC-1).
3. Update the actor's ``state`` and derived ``lifecycle_phase``.
4. Record an Operation (the provenance record) **and** append a ``state_change``
   Event (the audit record) — every transition writes both (INV-LC-2).

The caller owns the transaction (via ``org_scoped_session``), so the state
update, the Operation, and the Event commit or roll back together: there is no
window in which a transition is visible without its provenance, and a failed
Event write undoes the state change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.domain.lifecycle import (
    ACTOR_LIFECYCLE,
    IllegalTransitionError,
    phase_for_state,
)
from xiosync.persistence.models.identity import Actor
from xiosync.persistence.operations import OperationRepository
from xiosync.services.events import EventService
from xiosync.services.operations import OperationService

__all__ = [
    "ActorNotFoundError",
    "IllegalTransitionError",
    "LifecycleService",
    "LifecycleTransition",
]

#: The Operation ``operation_type`` recorded for an actor lifecycle transition.
_ACTOR_STATE_CHANGE_OPERATION = "actor.state_change"


class ActorNotFoundError(Exception):
    """The actor being transitioned does not exist in this organization."""

    def __init__(self, actor_id: uuid.UUID) -> None:
        super().__init__(f"actor {actor_id} not found in organization")
        self.actor_id = actor_id


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    """The outcome of a committed transition, including its provenance ids."""

    actor_id: uuid.UUID
    from_state: str
    to_state: str
    lifecycle_phase: str
    operation_id: uuid.UUID
    event_id: uuid.UUID


class LifecycleService:
    """Use-case orchestration for actor lifecycle transitions (doc 04 §2.1)."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._operations = OperationService(OperationRepository(session))
        self._events = EventService(session)

    def transition_actor(
        self,
        context: OrgContext,
        *,
        actor_id: uuid.UUID,
        to_state: str,
        trigger: str = "system",
        initiated_by: uuid.UUID | None = None,
        rationale: str | None = None,
    ) -> LifecycleTransition:
        """Move an actor to ``to_state``, enforcing legality and provenance.

        Rejects an unknown actor (``ActorNotFoundError``) and any transition the
        actor state machine does not declare (``IllegalTransitionError``,
        INV-LC-1) before mutating anything. On success it updates the actor,
        records an Operation, and appends a ``state_change`` Event (INV-LC-2),
        returning the ids of both provenance rows.
        """
        actor = self._session.scalar(
            select(Actor).where(
                Actor.organization_id == context.organization_id,
                Actor.id == actor_id,
            )
        )
        if actor is None:
            raise ActorNotFoundError(actor_id)

        from_state = actor.state
        # INV-LC-1: reject illegal transitions before any write.
        ACTOR_LIFECYCLE.assert_transition(from_state, to_state)
        new_phase = phase_for_state(to_state)

        # Apply the state change within the caller's transaction.
        self._session.execute(
            update(Actor)
            .where(
                Actor.organization_id == context.organization_id,
                Actor.id == actor_id,
            )
            .values(state=to_state, lifecycle_phase=new_phase, updated_at=func.now())
        )
        self._session.flush()

        # INV-LC-2: every transition writes an Operation ...
        operation_id = self._operations.record_operation(
            context,
            actor_id=actor_id,
            operation=_ACTOR_STATE_CHANGE_OPERATION,
            trigger=trigger,
            initiated_by=initiated_by or context.actor_id,
            from_state=from_state,
            to_state=to_state,
            scope="actor",
            rationale=rationale,
            outcome="success",
        )
        # ... and a state_change Event that references it.
        event_id = self._events.append_state_change(
            context,
            actor_id=actor_id,
            from_state=from_state,
            to_state=to_state,
            operation_id=operation_id,
            trigger=trigger,
            rationale=rationale,
        )

        return LifecycleTransition(
            actor_id=actor_id,
            from_state=from_state,
            to_state=to_state,
            lifecycle_phase=new_phase,
            operation_id=operation_id,
            event_id=event_id,
        )
