"""Operation use cases — hierarchy acyclicity enforced on write (doc 03 §§2.6, 3).

``OperationService`` orchestrates the pure hierarchy predicate
(``domain/operations``) over a tenant-scoped repository. INV-OP-1 (the
``parent_operation_id`` graph is an acyclic tree) is enforced *here*, on every
write that sets a parent — the schema cannot express it. INV-OP-2 (all
referenced operations share the org) is guaranteed by the composite FK and by
resolving parents only within the org-scoped parent map.

The caller owns the transaction (via ``org_scoped_session``); this service does
the read-validate-write sequence inside it, so the acyclicity decision and the
write commit or roll back atomically together.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from xiosync.domain.context import OrgContext
from xiosync.domain.operations import (
    HierarchyCycleError,
    ancestor_depth,
    would_create_cycle,
)
from xiosync.persistence.operations import OperationRepository

__all__ = [
    "HierarchyCycleError",
    "OperationNotFoundError",
    "OperationService",
    "ParentOperationNotFoundError",
]


class OperationNotFoundError(Exception):
    """The operation being reparented does not exist in this organization."""

    def __init__(self, operation_id: uuid.UUID) -> None:
        super().__init__(f"operation {operation_id} not found in organization")
        self.operation_id = operation_id


class ParentOperationNotFoundError(Exception):
    """The referenced parent operation is absent from this organization.

    Because the parent is resolved only within the org-scoped parent map, a
    cross-org parent is indistinguishable from a missing one — both are
    rejected, upholding INV-OP-2 at the service boundary.
    """

    def __init__(self, parent_operation_id: uuid.UUID) -> None:
        super().__init__(
            f"parent operation {parent_operation_id} not found in organization (INV-OP-2)"
        )
        self.parent_operation_id = parent_operation_id


class OperationService:
    """Use-case orchestration for Operations (doc 04 §2.1)."""

    def __init__(self, repository: OperationRepository) -> None:
        self._repository = repository

    def record_operation(
        self,
        context: OrgContext,
        *,
        actor_id: uuid.UUID,
        operation: str,
        trigger: str,
        initiated_by: uuid.UUID,
        from_state: str | None = None,
        to_state: str | None = None,
        collaborators: Sequence[Mapping[str, Any]] | None = None,
        scope: str | None = None,
        parent_operation_id: uuid.UUID | None = None,
        artifacts: Mapping[str, Any] | None = None,
        rationale: str | None = None,
        outcome: str | None = None,
    ) -> uuid.UUID:
        """Record a new Operation, optionally under a parent.

        A new node cannot itself close a cycle, but attaching it still requires
        the parent to exist in this org (INV-OP-2); the derived ``depth_level``
        comes from the parent's position in the hierarchy.
        """
        depth_level = 0
        if parent_operation_id is not None:
            parent_of = self._repository.load_parent_map(context)
            if parent_operation_id not in parent_of:
                raise ParentOperationNotFoundError(parent_operation_id)
            depth_level = ancestor_depth(parent_operation_id, parent_of) + 1

        return self._repository.insert_operation(
            context,
            actor_id=actor_id,
            operation=operation,
            trigger=trigger,
            initiated_by=initiated_by,
            from_state=from_state,
            to_state=to_state,
            collaborators=[dict(item) for item in (collaborators or [])],
            scope=scope,
            depth_level=depth_level,
            parent_operation_id=parent_operation_id,
            artifacts=None if artifacts is None else dict(artifacts),
            rationale=rationale,
            outcome=outcome,
        )

    def set_parent(
        self,
        context: OrgContext,
        operation_id: uuid.UUID,
        new_parent_id: uuid.UUID,
    ) -> None:
        """Reparent an operation, rejecting any change that cycles (INV-OP-1)."""
        if self._repository.get_operation(context, operation_id) is None:
            raise OperationNotFoundError(operation_id)

        parent_of = self._repository.load_parent_map(context)
        if new_parent_id not in parent_of:
            raise ParentOperationNotFoundError(new_parent_id)

        if would_create_cycle(
            node_id=operation_id, new_parent_id=new_parent_id, parent_of=parent_of
        ):
            raise HierarchyCycleError(operation_id, new_parent_id)

        depth_level = ancestor_depth(new_parent_id, parent_of) + 1
        if not self._repository.set_parent(
            context,
            operation_id,
            parent_operation_id=new_parent_id,
            depth_level=depth_level,
        ):
            raise OperationNotFoundError(operation_id)
