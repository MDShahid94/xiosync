"""Tenant-scoped persistence for Edges and Memory (docs 03 §§2.7, 2.9; 06 §5).

Every method takes ``OrgContext`` first (INV-TENANT-3) and filters by
``context.organization_id``; the RLS GUC bound by ``org_scoped_session`` is the
backstop (doc 05 §3.2). Records are returned detached from the ORM.

Same-org referential integrity (INV-EDGE-1 / INV-MEM-1) is guaranteed at the
schema by composite ``(organization_id, <actor_id>)`` FKs; ``actor_exists``
lets the service reject a dangling reference with a domain error *before* the
insert rather than surfacing a raw ``IntegrityError``. Edge adjacency is
materialized per org + graph class so the acyclicity check (INV-EDGE-2) never
leaves the tenant scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.identity import Actor
from xiosync.persistence.models.ontology import Edge, Memory
from xiosync.platform.ids import new_id


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    """One ``edges`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    edge_type: str
    graph_class: str
    weight: float | None
    state: str


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One ``memory`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    owner_actor_id: uuid.UUID
    kind: str
    content: dict[str, Any]
    visibility: str
    provenance: dict[str, Any] | None
    embedding_ref: str | None
    version: int
    superseded_by: uuid.UUID | None
    created_at: datetime


def _edge_record(row: Edge) -> EdgeRecord:
    return EdgeRecord(
        id=row.id,
        organization_id=row.organization_id,
        source_id=row.source_id,
        target_id=row.target_id,
        edge_type=row.edge_type,
        graph_class=row.graph_class,
        weight=row.weight,
        state=row.state,
    )


def _memory_record(row: Memory) -> MemoryRecord:
    return MemoryRecord(
        id=row.id,
        organization_id=row.organization_id,
        owner_actor_id=row.owner_actor_id,
        kind=row.kind,
        content=row.content,
        visibility=row.visibility,
        provenance=row.provenance,
        embedding_ref=row.embedding_ref,
        version=row.version,
        superseded_by=row.superseded_by,
        created_at=row.created_at,
    )


class EdgeRepository:
    """All database access for Edges within one tenant scope."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def actor_exists(self, context: OrgContext, actor_id: uuid.UUID) -> bool:
        return (
            self._session.scalar(
                select(func.count())
                .select_from(Actor)
                .where(
                    Actor.organization_id == context.organization_id,
                    Actor.id == actor_id,
                )
            )
            or 0
        ) > 0

    def load_adjacency(
        self, context: OrgContext, graph_class: str
    ) -> dict[uuid.UUID, set[uuid.UUID]]:
        """Source → {targets} for active edges of one org + graph class."""
        rows = self._session.execute(
            select(Edge.source_id, Edge.target_id).where(
                Edge.organization_id == context.organization_id,
                Edge.graph_class == graph_class,
                Edge.state == "active",
            )
        ).all()
        adjacency: dict[uuid.UUID, set[uuid.UUID]] = {}
        for source_id, target_id in rows:
            adjacency.setdefault(source_id, set()).add(target_id)
        return adjacency

    def insert_edge(
        self,
        context: OrgContext,
        *,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        edge_type: str,
        graph_class: str,
        weight: float | None,
        state: str,
    ) -> uuid.UUID:
        edge_id = new_id()
        self._session.add(
            Edge(
                id=edge_id,
                organization_id=context.organization_id,
                source_id=source_id,
                target_id=target_id,
                edge_type=edge_type,
                graph_class=graph_class,
                weight=weight,
                state=state,
            )
        )
        self._session.flush()
        return edge_id

    def get_edge(self, context: OrgContext, edge_id: uuid.UUID) -> EdgeRecord | None:
        row = self._session.scalar(
            select(Edge).where(
                Edge.organization_id == context.organization_id,
                Edge.id == edge_id,
            )
        )
        return None if row is None else _edge_record(row)


class MemoryRepository:
    """All database access for Memory within one tenant scope."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def actor_exists(self, context: OrgContext, actor_id: uuid.UUID) -> bool:
        return (
            self._session.scalar(
                select(func.count())
                .select_from(Actor)
                .where(
                    Actor.organization_id == context.organization_id,
                    Actor.id == actor_id,
                )
            )
            or 0
        ) > 0

    def get_memory(self, context: OrgContext, memory_id: uuid.UUID) -> MemoryRecord | None:
        row = self._session.scalar(
            select(Memory).where(
                Memory.organization_id == context.organization_id,
                Memory.id == memory_id,
            )
        )
        return None if row is None else _memory_record(row)

    def insert_memory(
        self,
        context: OrgContext,
        *,
        owner_actor_id: uuid.UUID,
        kind: str,
        content: dict[str, Any],
        visibility: str,
        provenance: dict[str, Any] | None,
        embedding_ref: str | None,
        version: int,
    ) -> uuid.UUID:
        memory_id = new_id()
        self._session.add(
            Memory(
                id=memory_id,
                organization_id=context.organization_id,
                owner_actor_id=owner_actor_id,
                kind=kind,
                content=content,
                visibility=visibility,
                provenance=provenance,
                embedding_ref=embedding_ref,
                version=version,
                superseded_by=None,
            )
        )
        self._session.flush()
        return memory_id

    def set_superseded_by(
        self, context: OrgContext, memory_id: uuid.UUID, successor_id: uuid.UUID
    ) -> bool:
        """Point a prior version at its successor; returns whether a row updated.

        Only rows not already superseded are touched, so a lost update cannot
        silently repoint an already-superseded version (INV-MEM-2).
        """
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(Memory)
                .where(
                    Memory.organization_id == context.organization_id,
                    Memory.id == memory_id,
                    Memory.superseded_by.is_(None),
                )
                .values(superseded_by=successor_id)
            ),
        )
        self._session.flush()
        return result.rowcount == 1
