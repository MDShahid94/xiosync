"""Edge & Memory use cases — same-org integrity + acyclicity + versioning.

``EdgeService`` and ``MemoryService`` orchestrate the pure ontology predicates
(``domain/ontology``) over tenant-scoped repositories, enforcing on write:

* **INV-EDGE-1 / INV-MEM-1** — an edge's endpoints and a memory's owner must be
  actors in the same organization. The composite FKs make a cross-org reference
  structurally impossible; the service checks first so the caller gets a clean
  domain error instead of a raw ``IntegrityError``.
* **INV-EDGE-2** — edges in an acyclic graph class (``hierarchy``, ``workflow``,
  ``dependency``) are rejected if they would close a cycle.
* **INV-MEM-2** — a memory is never mutated in place: ``update_memory`` writes a
  new version row and points the prior row's ``superseded_by`` at it.

The caller owns the transaction (via ``org_scoped_session``), so every
read-validate-write sequence here commits or rolls back atomically.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from xiosync.domain.context import OrgContext
from xiosync.domain.ontology import (
    GraphCycleError,
    UnknownGraphClassError,
    graph_class_is_acyclic,
    next_version,
    validate_graph_class,
    would_create_cycle,
)
from xiosync.persistence.ontology import EdgeRepository, MemoryRepository

__all__ = [
    "ActorNotInOrganizationError",
    "EdgeService",
    "GraphCycleError",
    "MemoryAlreadySupersededError",
    "MemoryNotFoundError",
    "MemoryService",
    "UnknownGraphClassError",
]


class ActorNotInOrganizationError(Exception):
    """A referenced actor is not in this organization (INV-EDGE-1 / INV-MEM-1)."""

    def __init__(self, actor_id: uuid.UUID, role: str) -> None:
        super().__init__(
            f"{role} actor {actor_id} is not in this organization (same-org referential integrity)"
        )
        self.actor_id = actor_id
        self.role = role


class MemoryNotFoundError(Exception):
    """The memory being updated does not exist in this organization."""

    def __init__(self, memory_id: uuid.UUID) -> None:
        super().__init__(f"memory {memory_id} not found in organization")
        self.memory_id = memory_id


class MemoryAlreadySupersededError(Exception):
    """The memory being updated has already been superseded (INV-MEM-2)."""

    def __init__(self, memory_id: uuid.UUID) -> None:
        super().__init__(
            f"memory {memory_id} is already superseded; update its latest version instead"
        )
        self.memory_id = memory_id


class EdgeService:
    """Use-case orchestration for Edges (doc 04 §2.1)."""

    def __init__(self, repository: EdgeRepository) -> None:
        self._repository = repository

    def create_edge(
        self,
        context: OrgContext,
        *,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        edge_type: str,
        graph_class: str,
        weight: float | None = None,
        state: str = "active",
    ) -> uuid.UUID:
        """Create an edge, enforcing the graph class, same-org endpoints, and
        per-class acyclicity.

        The order matters: the ``graph_class`` must be one of the four
        recognized classes (doc 03 §3) before anything else, then both
        endpoints must be actors in this organization (INV-EDGE-1), then — for
        an acyclic class — the edge must not close a cycle (INV-EDGE-2).
        """
        validate_graph_class(graph_class)

        if not self._repository.actor_exists(context, source_id):
            raise ActorNotInOrganizationError(source_id, "source")
        if not self._repository.actor_exists(context, target_id):
            raise ActorNotInOrganizationError(target_id, "target")

        if graph_class_is_acyclic(graph_class):
            adjacency = self._repository.load_adjacency(context, graph_class)
            if would_create_cycle(source_id=source_id, target_id=target_id, adjacency=adjacency):
                raise GraphCycleError(source_id, target_id, graph_class)

        return self._repository.insert_edge(
            context,
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            graph_class=graph_class,
            weight=weight,
            state=state,
        )


class MemoryService:
    """Use-case orchestration for versioned Memory (doc 04 §2.1)."""

    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository

    def create_memory(
        self,
        context: OrgContext,
        *,
        owner_actor_id: uuid.UUID,
        kind: str,
        content: Mapping[str, Any],
        visibility: str,
        provenance: Mapping[str, Any] | None = None,
        embedding_ref: str | None = None,
    ) -> uuid.UUID:
        """Create version 1 of a memory owned by a same-org actor (INV-MEM-1)."""
        if not self._repository.actor_exists(context, owner_actor_id):
            raise ActorNotInOrganizationError(owner_actor_id, "owner")

        return self._repository.insert_memory(
            context,
            owner_actor_id=owner_actor_id,
            kind=kind,
            content=dict(content),
            visibility=visibility,
            provenance=None if provenance is None else dict(provenance),
            embedding_ref=embedding_ref,
            version=1,
        )

    def update_memory(
        self,
        context: OrgContext,
        memory_id: uuid.UUID,
        *,
        content: Mapping[str, Any] | None = None,
        kind: str | None = None,
        visibility: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        embedding_ref: str | None = None,
    ) -> uuid.UUID:
        """Supersede a memory: write a new version and repoint the old row.

        Returns the new version's id. The original is retained unchanged except
        for its ``superseded_by`` pointer (INV-MEM-2). Only fields explicitly
        provided change; the rest carry forward from the superseded row.
        """
        existing = self._repository.get_memory(context, memory_id)
        if existing is None:
            raise MemoryNotFoundError(memory_id)
        if existing.superseded_by is not None:
            raise MemoryAlreadySupersededError(memory_id)

        successor_id = self._repository.insert_memory(
            context,
            owner_actor_id=existing.owner_actor_id,
            kind=existing.kind if kind is None else kind,
            content=existing.content if content is None else dict(content),
            visibility=existing.visibility if visibility is None else visibility,
            provenance=(existing.provenance if provenance is None else dict(provenance)),
            embedding_ref=(existing.embedding_ref if embedding_ref is None else embedding_ref),
            version=next_version(existing.version),
        )

        if not self._repository.set_superseded_by(context, memory_id, successor_id):
            # Another writer superseded this version between our read and write.
            raise MemoryAlreadySupersededError(memory_id)

        return successor_id
