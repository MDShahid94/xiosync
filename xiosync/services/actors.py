"""Actor management service (Genesis Phase 0c — Gap G-2).

Provides CRUD operations for actors — the missing service layer that makes
actor registration governable through the protocol.

Before this service, actors could only be created via raw SQL seed scripts.
Now any authenticated principal with the ``actor.create`` capability can
register new actors through the API, and every creation is recorded as an
Operation and Event in the audit trail.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.identity import Actor
from xiosync.persistence.models.operations import Operation
from xiosync.platform.ids import new_id

logger = logging.getLogger(__name__)


class ActorNotFoundError(Exception):
    """Requested actor does not exist in this organization."""

    def __init__(self, actor_id: uuid.UUID) -> None:
        super().__init__(f"actor {actor_id} not found")
        self.actor_id = actor_id


class ActorAlreadyExistsError(Exception):
    """An actor with the given ID already exists in this organization."""

    def __init__(self, actor_id: uuid.UUID) -> None:
        super().__init__(f"actor {actor_id} already exists")
        self.actor_id = actor_id


@dataclass(frozen=True, slots=True)
class ActorRecord:
    """Read-only view of an Actor."""

    id: uuid.UUID
    organization_id: uuid.UUID
    actor_type: str
    actor_subtype: str | None
    role: str | None
    alias: str | None
    parent_id: uuid.UUID | None
    state: str
    lifecycle_phase: str
    trust_tier: str
    health_status: str
    config: dict[str, Any] | None
    runtime_state: dict[str, Any] | None
    created_by: uuid.UUID | None
    created_at: datetime

    @classmethod
    def from_model(cls, actor: Actor) -> ActorRecord:
        return cls(
            id=actor.id,
            organization_id=actor.organization_id,
            actor_type=actor.actor_type,
            actor_subtype=actor.actor_subtype,
            role=actor.role,
            alias=actor.alias,
            parent_id=actor.parent_id,
            state=actor.state,
            lifecycle_phase=actor.lifecycle_phase,
            trust_tier=actor.trust_tier,
            health_status=actor.health_status,
            config=actor.config,
            runtime_state=actor.runtime_state,
            created_by=actor.created_by,
            created_at=actor.created_at,
        )


class ActorService:
    """CRUD operations for actors with full audit trail.

    Every mutation is recorded as an Operation and Event, making actor
    management a governed activity within the XIOSYNC protocol.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_actor(
        self,
        context: OrgContext,
        *,
        actor_type: str,
        actor_subtype: str | None = None,
        role: str | None = None,
        alias: str | None = None,
        parent_id: uuid.UUID | None = None,
        state: str = "active",
        lifecycle_phase: str = "operational",
        trust_tier: str = "newcomer",
        health_status: str = "healthy",
        config: dict[str, Any] | None = None,
    ) -> ActorRecord:
        """Create a new actor in the current organization.

        Records the creation as an Operation and Event in the audit trail.
        """
        actor_id = new_id()
        from datetime import UTC
        now = datetime.now(UTC)

        actor = Actor(
            id=actor_id,
            organization_id=context.organization_id,
            actor_type=actor_type,
            actor_subtype=actor_subtype,
            role=role,
            alias=alias,
            parent_id=parent_id,
            state=state,
            lifecycle_phase=lifecycle_phase,
            trust_tier=trust_tier,
            health_status=health_status,
            config=config,
            created_by=context.actor_id,
            created_at=now,
        )
        self._session.add(actor)
        self._session.flush()

        # Record operation
        op_id = new_id()
        op = Operation(
            id=op_id,
            organization_id=context.organization_id,
            actor_id=context.actor_id,
            operation="actor.create",
            trigger="user_command",
            initiated_by=context.actor_id,
            scope="actor",
            outcome="success",
            rationale=f"Created {actor_type} actor: {alias or actor_id}",
            started_at=now,
            completed_at=now,
        )
        self._session.add(op)

        # Record event
        event = Event(
            id=new_id(),
            organization_id=context.organization_id,
            event_type="actor.created",
            actor_id=context.actor_id,
            severity="info",
            operation_id=op_id,
            entity_type="actor",
            entity_id=actor_id,
            payload={
                "summary": f"Actor {actor_id} ({actor_type}) created",
                "actor_id": str(actor_id),
                "actor_type": actor_type,
                "actor_subtype": actor_subtype,
                "alias": alias,
                "created_by": str(context.actor_id),
            },
            created_at=now,
        )
        self._session.add(event)
        self._session.flush()

        logger.info(
            "actor.created: id=%s type=%s org=%s",
            actor_id,
            actor_type,
            context.organization_id,
        )
        return ActorRecord.from_model(actor)

    def get_actor(
        self,
        context: OrgContext,
        actor_id: uuid.UUID,
    ) -> ActorRecord:
        """Get an actor by ID within the current organization."""
        actor = self._session.execute(
            select(Actor).where(
                Actor.id == actor_id,
                Actor.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()

        if actor is None:
            raise ActorNotFoundError(actor_id)
        return ActorRecord.from_model(actor)

    def list_actors(
        self,
        context: OrgContext,
        *,
        actor_type: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[ActorRecord]:
        """List actors in the current organization with optional filters."""
        query = select(Actor).where(
            Actor.organization_id == context.organization_id
        )
        if actor_type is not None:
            query = query.where(Actor.actor_type == actor_type)
        if state is not None:
            query = query.where(Actor.state == state)
        query = query.order_by(Actor.created_at.desc()).limit(limit)

        actors = self._session.execute(query).scalars().all()
        return [ActorRecord.from_model(a) for a in actors]

    def count_actors(
        self,
        context: OrgContext,
        *,
        actor_type: str | None = None,
    ) -> int:
        """Count actors in the current organization."""
        query = select(func.count(Actor.id)).where(
            Actor.organization_id == context.organization_id
        )
        if actor_type is not None:
            query = query.where(Actor.actor_type == actor_type)
        result = self._session.execute(query).scalar()
        return result or 0
