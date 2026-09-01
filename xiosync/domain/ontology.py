"""Pure ontology-graph semantics for Edges and Memory (doc 03 §§2.7, 2.9, 3).

Two concerns live here, both framework-free (RULE-ARCH-1):

* **Edge acyclicity per graph class (INV-EDGE-2, doc 03 §3).** XIOSYNC has four
  graph classes and it is false to call the whole graph a DAG. ``hierarchy``
  and ``workflow`` are acyclic; ``dependency`` edges (``delegates_to``,
  ``manages``, ``owns``) are acyclic per their semantics; ``relationship``
  edges (``collaborates_with``, ``provides``) may freely contain cycles. The
  acyclic classes are validated on write via the pure predicate below, over an
  adjacency map a repository materializes for the current org + graph class.

* **Memory versioning (INV-MEM-2, doc 03 §2.9).** Memory is never overwritten:
  an update writes a new row at ``version + 1`` and the prior row's
  ``superseded_by`` is pointed at it. The successor version number is pure
  arithmetic, expressed here so the service layer holds no ontology rules.

The adjacency map only ever contains same-org ids (it is built inside the
tenant scope), so acyclicity reasoning and tenancy stay cleanly separated
(INV-EDGE-1 is enforced separately, at the service boundary and by the schema).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Set

#: The four — and only four — graph classes XIOSYNC recognizes (doc 03 §3).
#: An edge MUST declare one of these; any other value is rejected on write so
#: the closed set is enforced in the domain, not merely by the DB CHECK.
GRAPH_CLASS_HIERARCHY = "hierarchy"
GRAPH_CLASS_WORKFLOW = "workflow"
GRAPH_CLASS_RELATIONSHIP = "relationship"
GRAPH_CLASS_DEPENDENCY = "dependency"

#: The closed set of valid ``graph_class`` values (doc 03 §3). This mirrors the
#: ``ck_edges_graph_class_allowed`` schema CHECK; the domain is the first line
#: of defense so callers get a clean domain error, not a raw ``IntegrityError``.
GRAPH_CLASSES: frozenset[str] = frozenset(
    {
        GRAPH_CLASS_HIERARCHY,
        GRAPH_CLASS_WORKFLOW,
        GRAPH_CLASS_RELATIONSHIP,
        GRAPH_CLASS_DEPENDENCY,
    }
)

#: Graph classes whose edges must form a DAG, validated on write (doc 03 §3).
#: ``relationship`` is intentionally absent — cycles are legal there. Per doc
#: 03 §3 the acyclic classes are ``hierarchy`` and ``workflow`` (strictly
#: acyclic) and ``dependency`` (acyclic per its edge-type semantics).
ACYCLIC_GRAPH_CLASSES: frozenset[str] = frozenset(
    {
        GRAPH_CLASS_HIERARCHY,
        GRAPH_CLASS_WORKFLOW,
        GRAPH_CLASS_DEPENDENCY,
    }
)


class UnknownGraphClassError(Exception):
    """An edge declared a ``graph_class`` outside the closed set (doc 03 §3)."""

    def __init__(self, graph_class: str) -> None:
        allowed = ", ".join(sorted(GRAPH_CLASSES))
        super().__init__(
            f"unknown graph_class {graph_class!r}: an edge must declare one of "
            f"{{{allowed}}} (doc 03 §3)"
        )
        self.graph_class = graph_class


class GraphCycleError(Exception):
    """Adding an edge would create a cycle in an acyclic graph class (INV-EDGE-2)."""

    def __init__(self, source_id: uuid.UUID, target_id: uuid.UUID, graph_class: str) -> None:
        super().__init__(
            f"edge {source_id} -> {target_id} rejected: it would create a cycle in "
            f"the acyclic {graph_class!r} graph (INV-EDGE-2)"
        )
        self.source_id = source_id
        self.target_id = target_id
        self.graph_class = graph_class


def validate_graph_class(graph_class: str) -> None:
    """Reject a ``graph_class`` outside the four recognized classes (doc 03 §3).

    Enforcing the closed set here means a caller learns a bad value is invalid
    through a domain error before any persistence work, and the four classes
    are enforced independently of the backing schema's CHECK constraint.
    """
    if graph_class not in GRAPH_CLASSES:
        raise UnknownGraphClassError(graph_class)


def graph_class_is_acyclic(graph_class: str) -> bool:
    """Whether ``graph_class`` forbids cycles and must be checked on write."""
    return graph_class in ACYCLIC_GRAPH_CLASSES


def would_create_cycle(
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    adjacency: Mapping[uuid.UUID, Set[uuid.UUID]],
) -> bool:
    """Return whether adding directed edge ``source_id → target_id`` cycles.

    ``adjacency`` maps each node to the set of nodes it already points at
    within one org + graph class. The new edge closes a cycle exactly when
    ``source_id`` is reachable from ``target_id`` through existing edges — a
    forward walk from ``target_id`` that arrives back at ``source_id``. A
    self-loop (``source_id == target_id``) is the degenerate cycle.
    """
    if source_id == target_id:
        return True
    stack: list[uuid.UUID] = [target_id]
    visited: set[uuid.UUID] = set()
    while stack:
        node = stack.pop()
        if node == source_id:
            return True
        if node in visited:
            continue
        visited.add(node)
        stack.extend(adjacency.get(node, frozenset()))
    return False


def next_version(current_version: int) -> int:
    """The successor version for a superseding Memory row (INV-MEM-2)."""
    return current_version + 1
