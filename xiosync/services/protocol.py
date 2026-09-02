"""Protocol evolution tracking service (Phase 3 — Gap G-5).

XIOSYNC is an evolutionary entity. Every protocol change, schema migration,
capability addition, and configuration change is tracked as a first-class
versioned record.  This service provides:

1. **Protocol versions** — semantic version tracking of the protocol itself
2. **Evolution records** — every change with rationale, author, and diff
3. **Schema migration tracking** — migrations as governed protocol events
4. **Configuration drift detection** — track what changed and when

The protocol itself evolves through its own governance mechanism:
every evolution is recorded as an Operation + Event, making the
evolution history auditable and reproducible.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.ontology import TypeRegistry
from xiosync.persistence.models.operations import Operation
from xiosync.platform.ids import new_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProtocolVersion:
    """A snapshot of the protocol at a point in time."""

    version: str  # semver: "0.1.0"
    description: str
    changes: list[dict[str, Any]]
    previous_version: str | None
    author_actor_id: uuid.UUID
    operation_id: uuid.UUID
    event_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvolutionRecord:
    """A single tracked change in the protocol."""

    id: uuid.UUID
    evolution_type: str  # "schema_migration", "capability_added", "config_change", "protocol_upgrade"
    description: str
    rationale: str | None
    diff: dict[str, Any]  # structured diff of what changed
    actor_id: uuid.UUID
    operation_id: uuid.UUID
    event_id: uuid.UUID
    created_at: datetime


class ProtocolService:
    """Tracks XIOSYNC protocol evolution as a governed activity.

    Every protocol change is recorded as:
    - A type_registry entry (category: "protocol_version")
    - An Operation (who, when, what)
    - An Event (audit trail)

    This makes the protocol evolution itself subject to the protocol.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record_evolution(
        self,
        context: OrgContext,
        *,
        evolution_type: str,
        description: str,
        rationale: str | None = None,
        diff: dict[str, Any] | None = None,
        version: str | None = None,
    ) -> EvolutionRecord:
        """Record a protocol evolution event.

        Args:
            evolution_type: One of "schema_migration", "capability_added",
                "capability_deprecated", "config_change", "protocol_upgrade",
                "type_registered", "category_added".
            description: Human-readable description of what changed.
            rationale: Why this change was made.
            diff: Structured diff of what changed (before/after).
            version: Optional new protocol version (semver).
        """
        now = datetime.now(UTC)
        evolution_id = new_id()
        op_id = new_id()
        event_id = new_id()

        # Record the operation
        op = Operation(
            id=op_id,
            organization_id=context.organization_id,
            actor_id=context.actor_id,
            operation="protocol.evolve",
            trigger="system",
            initiated_by=context.actor_id,
            scope="organization",
            outcome="success",
            rationale=rationale or description,
            started_at=now,
            completed_at=now,
        )
        self._session.add(op)

        # Record the event
        event = Event(
            id=event_id,
            organization_id=context.organization_id,
            event_type="protocol.evolution",
            actor_id=context.actor_id,
            severity="info",
            operation_id=op_id,
            entity_type="protocol",
            entity_id=evolution_id,
            payload={
                "evolution_type": evolution_type,
                "description": description,
                "rationale": rationale,
                "diff": diff or {},
                "version": version,
            },
            created_at=now,
        )
        self._session.add(event)

        # If a version bump, register it in the type registry
        if version:
            registry_entry = TypeRegistry(
                id=new_id(),
                organization_id=context.organization_id,
                namespace="core",
                category="meta_category",
                value=f"protocol.version.{version}",
                state="active",
                definition={"description": description, "diff": diff or {}},
            )
            self._session.add(registry_entry)

        self._session.flush()

        logger.info(
            "protocol.evolved: type=%s desc='%s' version=%s actor=%s",
            evolution_type, description, version, context.actor_id,
        )

        return EvolutionRecord(
            id=evolution_id,
            evolution_type=evolution_type,
            description=description,
            rationale=rationale,
            diff=diff or {},
            actor_id=context.actor_id,
            operation_id=op_id,
            event_id=event_id,
            created_at=now,
        )

    def record_schema_migration(
        self,
        context: OrgContext,
        *,
        migration_id: str,
        description: str,
        tables_affected: list[str],
        direction: str = "upgrade",
    ) -> EvolutionRecord:
        """Record a schema migration as a protocol evolution event."""
        return self.record_evolution(
            context,
            evolution_type="schema_migration",
            description=f"Migration {migration_id}: {description}",
            rationale=f"Schema {direction} — {migration_id}",
            diff={
                "migration_id": migration_id,
                "direction": direction,
                "tables_affected": tables_affected,
            },
        )

    def get_evolution_history(
        self,
        context: OrgContext,
        *,
        evolution_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query the protocol evolution history from the events table."""
        stmt = (
            select(Event)
            .where(
                Event.organization_id == context.organization_id,
                Event.event_type == "protocol.evolution",
            )
            .order_by(Event.created_at.desc())
            .limit(limit)
        )
        if evolution_type:
            # Filter by evolution_type in payload JSONB
            stmt = stmt.where(
                Event.payload["evolution_type"].as_string() == evolution_type
            )
        rows = self._session.scalars(stmt).all()
        return [
            {
                "id": str(r.id),
                "evolution_type": r.payload.get("evolution_type"),
                "description": r.payload.get("description"),
                "version": r.payload.get("version"),
                "actor_id": str(r.actor_id) if r.actor_id else None,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    def get_current_version(self, context: OrgContext) -> str | None:
        """Get the latest protocol version from type_registry."""
        row = self._session.scalar(
            select(TypeRegistry.value)
            .where(
                TypeRegistry.category == "meta_category",
                TypeRegistry.value.like("protocol.version.%"),
            )
            .order_by(TypeRegistry.created_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        return str(row).replace("protocol.version.", "")
