"""Pure Operation-hierarchy semantics (doc 03 §§2.6, 3).

The Operation ``parent_operation_id`` self reference is the *hierarchy* graph;
it MUST stay acyclic (INV-OP-1). The schema deliberately does not (and cannot
cheaply) enforce this — a composite FK guarantees same-org parenting
(INV-OP-2) but not acyclicity — so the rule is validated on write in the
service layer, using the pure predicate defined here.

This module is pure domain (RULE-ARCH-1): no I/O, no framework imports. It
operates on a plain ``parent_of`` mapping (operation id → its parent id, or
``None`` for a root) that a repository materializes for the current
organization. Cross-org ids never appear in that map because the map is built
inside the tenant scope, so acyclicity and tenancy stay cleanly separated.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping


class HierarchyCycleError(Exception):
    """Attaching a parent would create a cycle in the hierarchy graph (INV-OP-1)."""

    def __init__(self, node_id: uuid.UUID, parent_id: uuid.UUID) -> None:
        super().__init__(
            f"operation {node_id} cannot have parent {parent_id}: "
            "the hierarchy graph would contain a cycle (INV-OP-1)"
        )
        self.node_id = node_id
        self.parent_id = parent_id


def would_create_cycle(
    *,
    node_id: uuid.UUID,
    new_parent_id: uuid.UUID,
    parent_of: Mapping[uuid.UUID, uuid.UUID | None],
) -> bool:
    """Return whether setting ``node_id``'s parent to ``new_parent_id`` cycles.

    The hierarchy is a set of parent pointers. Adding the edge
    ``node_id → new_parent_id`` (child points at parent) closes a cycle exactly
    when ``node_id`` is already an ancestor of ``new_parent_id`` — i.e. walking
    upward from ``new_parent_id`` reaches ``node_id``. A self-parent
    (``node_id == new_parent_id``) is the degenerate one-node cycle.

    The walk carries its own ``visited`` guard so a pre-existing corrupt chain
    (which should be impossible, but is not trusted) terminates instead of
    looping forever, and is reported as a cycle.
    """
    if node_id == new_parent_id:
        return True
    visited: set[uuid.UUID] = set()
    cursor: uuid.UUID | None = new_parent_id
    while cursor is not None:
        if cursor == node_id:
            return True
        if cursor in visited:
            # A cycle that does not involve node_id already exists upstream;
            # treat it as unsafe rather than spin forever.
            return True
        visited.add(cursor)
        cursor = parent_of.get(cursor)
    return False


def ancestor_depth(node_id: uuid.UUID, parent_of: Mapping[uuid.UUID, uuid.UUID | None]) -> int:
    """Number of ancestors above ``node_id`` (a root has depth ``0``).

    Used to derive an Operation's ``depth_level`` from its parent chain. The
    same ``visited`` guard bounds a corrupt chain instead of looping.
    """
    depth = 0
    visited: set[uuid.UUID] = set()
    cursor: uuid.UUID | None = parent_of.get(node_id)
    while cursor is not None and cursor not in visited:
        visited.add(cursor)
        depth += 1
        cursor = parent_of.get(cursor)
    return depth
