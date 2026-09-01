"""Pure Workflow / WorkflowRun / Task / DeadLetter semantics (docs 03 §§2.11, 3; 07).

Three framework-free concerns live here (RULE-ARCH-1: no I/O, no framework
imports):

* **Workflow spec is a validated DAG (INV-WF-1, doc 03 §§2.11, 3).** A workflow
  ``spec`` describes a *workflow-class* graph (doc 03 §3) whose nodes/edges MUST
  be acyclic. Publishing a cyclic — or otherwise malformed — spec is rejected.
  The validation is expressed as a pure predicate over the plain ``spec``
  mapping so the service layer holds no graph rules and the check runs before
  any persistence work. Full DAG validation happens *on publish* (doc 03 §3
  table: workflow graph "enforced on publish").

* **The closed state sets (doc 03 §§2.11, 4.4; doc 07 §1.1).** ``Workflow``,
  ``WorkflowRun``, ``Task`` and ``DeadLetter`` each have a schema-fixed closed
  set of states. These mirror the ``CHECK`` constraints in the models so a
  caller gets a clean domain error before hitting a raw ``IntegrityError``; the
  sets are the domain's first line of defense, independent of the DB CHECK.

* **The DLQ landing rule (INV-DLQ-1, doc 07 §4).** A failed task lands in
  ``dead_letters`` in state ``open`` and nothing auto-resolves it; the landing
  state is named here so both the model default and the service agree on it.

The spec shape validated here is deliberately minimal and self-describing::

    {
        "nodes": [{"id": "fetch"}, {"id": "transform"}, {"id": "load"}],
        "edges": [{"from": "fetch", "to": "transform"},
                  {"from": "transform", "to": "load"}],
    }

Each node is an object carrying at least a unique, non-empty string ``id``
(further node fields — capability ref, config — are opaque to acyclicity and
left to later validation stages). Each edge is an object with ``from``/``to``
referencing declared node ids. Only structure and acyclicity are enforced here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

# --- Closed state sets (docs 03 §§2.11, 4.4; 07 §1.1) ----------------------

#: A Workflow definition's lifecycle (doc 03 §2.11). ``published`` requires a
#: validated DAG (INV-WF-1); promotion of a corrected spec is a *new version*
#: (doc 06 §6 / INV-DLQ-4), never an in-place edit of a published spec.
WORKFLOW_STATE_DRAFT = "draft"
WORKFLOW_STATE_PUBLISHED = "published"
WORKFLOW_STATE_DEPRECATED = "deprecated"
WORKFLOW_STATES: frozenset[str] = frozenset(
    {WORKFLOW_STATE_DRAFT, WORKFLOW_STATE_PUBLISHED, WORKFLOW_STATE_DEPRECATED}
)

#: A single execution of a Workflow (doc 03 §§2.11, 4.4). ``failed`` may route
#: to the governed DLQ correction flow (doc 07 §4) — never auto-mutation.
WORKFLOW_RUN_STATE_QUEUED = "queued"
WORKFLOW_RUN_STATE_RUNNING = "running"
WORKFLOW_RUN_STATE_PAUSED = "paused"
WORKFLOW_RUN_STATE_SUCCEEDED = "succeeded"
WORKFLOW_RUN_STATE_FAILED = "failed"
WORKFLOW_RUN_STATE_CANCELLED = "cancelled"
WORKFLOW_RUN_STATES: frozenset[str] = frozenset(
    {
        WORKFLOW_RUN_STATE_QUEUED,
        WORKFLOW_RUN_STATE_RUNNING,
        WORKFLOW_RUN_STATE_PAUSED,
        WORKFLOW_RUN_STATE_SUCCEEDED,
        WORKFLOW_RUN_STATE_FAILED,
        WORKFLOW_RUN_STATE_CANCELLED,
    }
)

#: A task's lease lifecycle (doc 07 §1.1). ``dead_letter`` is the terminal state
#: for a task whose retries are exhausted; it routes to ``dead_letters`` (§4).
TASK_STATE_QUEUED = "queued"
TASK_STATE_LEASED = "leased"
TASK_STATE_COMPLETED = "completed"
TASK_STATE_FAILED = "failed"
TASK_STATE_EXPIRED = "expired"
TASK_STATE_DEAD_LETTER = "dead_letter"
TASK_STATES: frozenset[str] = frozenset(
    {
        TASK_STATE_QUEUED,
        TASK_STATE_LEASED,
        TASK_STATE_COMPLETED,
        TASK_STATE_FAILED,
        TASK_STATE_EXPIRED,
        TASK_STATE_DEAD_LETTER,
    }
)

#: A dead-letter record's governance state (doc 07 §4). INV-DLQ-1: it lands in
#: ``open`` and nothing auto-resolves it; advancing to ``resolved`` is a
#: human/policy-governed act (INV-DLQ-2/3), never a model side effect.
DEAD_LETTER_STATE_OPEN = "open"
DEAD_LETTER_STATE_INVESTIGATING = "investigating"
DEAD_LETTER_STATE_RESOLVED = "resolved"
DEAD_LETTER_STATES: frozenset[str] = frozenset(
    {
        DEAD_LETTER_STATE_OPEN,
        DEAD_LETTER_STATE_INVESTIGATING,
        DEAD_LETTER_STATE_RESOLVED,
    }
)

#: The state a failed task's dead-letter record is created in (INV-DLQ-1).
DEAD_LETTER_LANDING_STATE = DEAD_LETTER_STATE_OPEN


# --- Workflow spec DAG validation (INV-WF-1, doc 03 §§2.11, 3) -------------


class WorkflowSpecError(Exception):
    """A workflow ``spec`` is structurally invalid and cannot be published.

    Base class for every publish-time spec rejection so callers can catch the
    whole family (malformed shape, dangling edge, cycle) with one except.
    """


class WorkflowCycleError(WorkflowSpecError):
    """A workflow ``spec`` contains a cycle, violating INV-WF-1 (doc 03 §3).

    The workflow graph class is strictly acyclic; a spec whose nodes/edges close
    a cycle is rejected on publish.
    """

    def __init__(self, cycle: Sequence[str]) -> None:
        trail = " -> ".join(cycle)
        super().__init__(
            f"workflow spec is not a DAG: it contains a cycle ({trail}); "
            "publishing a cyclic spec is rejected (INV-WF-1, doc 03 §3)"
        )
        self.cycle = tuple(cycle)


def _node_ids(spec: Mapping[str, Any]) -> list[str]:
    """Extract and validate the spec's declared node ids.

    Enforces that ``nodes`` is a list of objects each carrying a unique,
    non-empty string ``id``. Structural problems are raised as
    ``WorkflowSpecError`` before any acyclicity work.
    """
    raw_nodes = spec.get("nodes")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise WorkflowSpecError("workflow spec must contain a 'nodes' list")
    if not raw_nodes:
        raise WorkflowSpecError("workflow spec must declare at least one node")

    ids: list[str] = []
    seen: set[str] = set()
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, Mapping):
            raise WorkflowSpecError(f"node at index {index} must be an object")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise WorkflowSpecError(f"node at index {index} must have a non-empty string 'id'")
        if node_id in seen:
            raise WorkflowSpecError(f"duplicate node id {node_id!r} in workflow spec")
        seen.add(node_id)
        ids.append(node_id)
    return ids


def _adjacency(spec: Mapping[str, Any], node_ids: set[str]) -> dict[str, set[str]]:
    """Build the ``from -> {to, ...}`` adjacency map, validating every edge.

    Edges must be objects referencing declared node ids via ``from``/``to``; a
    dangling reference or a self-loop is rejected (a self-loop is the degenerate
    one-node cycle).
    """
    raw_edges = spec.get("edges", [])
    if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, (str, bytes)):
        raise WorkflowSpecError("workflow spec 'edges' must be a list when present")

    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for index, edge in enumerate(raw_edges):
        if not isinstance(edge, Mapping):
            raise WorkflowSpecError(f"edge at index {index} must be an object")
        source = edge.get("from")
        target = edge.get("to")
        if not isinstance(source, str) or not isinstance(target, str):
            raise WorkflowSpecError(
                f"edge at index {index} must have string 'from' and 'to' fields"
            )
        if source not in node_ids:
            raise WorkflowSpecError(f"edge at index {index} references unknown node {source!r}")
        if target not in node_ids:
            raise WorkflowSpecError(f"edge at index {index} references unknown node {target!r}")
        if source == target:
            raise WorkflowCycleError((source, target))
        adjacency[source].add(target)
    return adjacency


def _find_cycle(adjacency: Mapping[str, set[str]]) -> list[str] | None:
    """Return one cycle as an ordered node list, or ``None`` if the graph is a DAG.

    Standard three-colour DFS: a back-edge to a node currently on the recursion
    stack (grey) closes a cycle, and the path from that node is reconstructed
    for a legible error. The colour map also guarantees termination on any
    input, so a pathological spec cannot loop the validator forever.
    """
    WHITE, GREY, BLACK = 0, 1, 2  # noqa: N806 — graph-theory colour convention
    colour: dict[str, int] = {node: WHITE for node in adjacency}

    def visit(node: str, path: list[str]) -> list[str] | None:
        colour[node] = GREY
        path.append(node)
        for neighbour in adjacency.get(node, ()):  # deterministic enough for reporting
            if colour[neighbour] == GREY:
                # Back-edge: the cycle is the path from neighbour to here, closed.
                start = path.index(neighbour)
                return [*path[start:], neighbour]
            if colour[neighbour] == WHITE:
                found = visit(neighbour, path)
                if found is not None:
                    return found
        path.pop()
        colour[node] = BLACK
        return None

    for node in adjacency:
        if colour[node] == WHITE:
            cycle = visit(node, [])
            if cycle is not None:
                return cycle
    return None


def validate_workflow_dag(spec: Mapping[str, Any]) -> None:
    """Validate a workflow ``spec`` is a well-formed DAG, or raise (INV-WF-1).

    This is the on-publish gate of doc 03 §3: the spec must be a mapping with a
    non-empty ``nodes`` list of uniquely-id'd node objects, every ``edges``
    entry must reference declared nodes, and the resulting directed graph must
    be acyclic. Raises :class:`WorkflowCycleError` for a cycle (a subclass of
    :class:`WorkflowSpecError`) and :class:`WorkflowSpecError` for any other
    structural defect. Returns ``None`` when the spec is a valid DAG.

    Gap T-2: also validates ``input_from`` data flow declarations on nodes.
    """
    if not isinstance(spec, Mapping):
        raise WorkflowSpecError("workflow spec must be an object")
    node_ids = _node_ids(spec)
    adjacency = _adjacency(spec, set(node_ids))
    cycle = _find_cycle(adjacency)
    if cycle is not None:
        raise WorkflowCycleError(cycle)
    # T-2: validate data flow declarations.
    validate_data_flow(spec, set(node_ids), adjacency)


# --- Gap T-2: DAG data flow (output → input wiring) -------------------------


class DataFlowError(WorkflowSpecError):
    """A ``spec`` node has an invalid ``input_from`` declaration (Gap T-2)."""


def _predecessors(adjacency: Mapping[str, set[str]], node_id: str) -> set[str]:
    """Return the set of all transitive predecessors of ``node_id``."""
    preds: set[str] = set()
    for src, targets in adjacency.items():
        if node_id in targets:
            preds.add(src)
            preds |= _predecessors(adjacency, src)
    return preds


def validate_data_flow(
    spec: Mapping[str, Any],
    node_ids: set[str],
    adjacency: Mapping[str, set[str]],
) -> None:
    """Validate ``input_from`` declarations on DAG nodes (Gap T-2).

    Supported forms:
    - Whole-result shorthand: ``{"input_from": "fetch"}``
      → entire upstream result becomes the input.
    - Key-level mapping: ``{"input_from": {"raw_data": "fetch.result.data"}}``
      → upstream ``result.data`` maps to downstream ``raw_data``.

    Validates:
    - Referenced upstream node IDs exist in the DAG.
    - Referenced nodes are topological predecessors (no forward references).
    - Key names are non-empty strings.
    """
    raw_nodes = spec.get("nodes", [])
    for node in raw_nodes:
        if not isinstance(node, Mapping):
            continue
        input_from = node.get("input_from")
        if input_from is None:
            continue

        node_id = node["id"]

        # Compute predecessor set for this node.
        preds = _predecessors(adjacency, node_id)

        if isinstance(input_from, str):
            # Whole-result shorthand: "input_from": "upstream_node_id"
            if input_from not in node_ids:
                raise DataFlowError(
                    f"node {node_id!r} input_from references unknown node {input_from!r}"
                )
            if input_from not in preds:
                raise DataFlowError(
                    f"node {node_id!r} input_from references {input_from!r} which is not "
                    f"a topological predecessor"
                )
        elif isinstance(input_from, Mapping):
            # Key-level mapping: "input_from": {"key": "upstream_node.result.field"}
            for key, source_path in input_from.items():
                if not isinstance(key, str) or not key:
                    raise DataFlowError(
                        f"node {node_id!r} input_from has invalid key {key!r}"
                    )
                if not isinstance(source_path, str) or not source_path:
                    raise DataFlowError(
                        f"node {node_id!r} input_from key {key!r} has invalid source path"
                    )
                # Extract the upstream node ID (first segment before '.')
                upstream_node = source_path.split(".")[0]
                if upstream_node not in node_ids:
                    raise DataFlowError(
                        f"node {node_id!r} input_from key {key!r} references unknown "
                        f"node {upstream_node!r}"
                    )
                if upstream_node not in preds:
                    raise DataFlowError(
                        f"node {node_id!r} input_from key {key!r} references "
                        f"{upstream_node!r} which is not a topological predecessor"
                    )
        else:
            raise DataFlowError(
                f"node {node_id!r} input_from must be a string or mapping, "
                f"got {type(input_from).__name__}"
            )


def resolve_node_inputs(
    input_from: str | Mapping[str, str],
    upstream_results: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve a node's ``input_from`` declaration against upstream task results.

    Gap T-2: called at runtime when an upstream task completes. Given the
    ``input_from`` spec and a mapping of ``{node_id: result_dict}``, produces
    the resolved input dict for the downstream task.

    Args:
        input_from: The node's ``input_from`` declaration (string or mapping).
        upstream_results: Mapping of ``node_id -> result`` for completed upstream tasks.

    Returns:
        The resolved input dict for the downstream task.
    """
    if isinstance(input_from, str):
        # Whole-result shorthand.
        result = upstream_results.get(input_from)
        if isinstance(result, Mapping):
            return dict(result)
        return {"_result": result}

    # Key-level mapping.
    resolved: dict[str, Any] = {}
    for key, source_path in input_from.items():
        parts = source_path.split(".")
        upstream_node = parts[0]
        value = upstream_results.get(upstream_node)

        # Traverse dotted path (e.g., "fetch.result.data" → parts[1:] = ["result", "data"])
        for segment in parts[1:]:
            if isinstance(value, Mapping):
                value = value.get(segment)
            else:
                value = None
                break
        resolved[key] = value

    return resolved


# --- Lease lifecycle predicates (INV-EXEC-1/2, doc 07 §1.1) -----------------

#: Only ``queued`` tasks may be leased (expired tasks return to queued before
#: re-leasing, so leasing is always from the queued state).
TASK_LEASEABLE_STATES: frozenset[str] = frozenset({TASK_STATE_QUEUED})

#: Tasks in these states may be completed by the worker that holds the lease.
TASK_COMPLETABLE_STATES: frozenset[str] = frozenset({TASK_STATE_LEASED})


def task_is_leaseable(state: str) -> bool:
    """Return ``True`` iff a task in *state* may have a lease acquired on it.

    Only ``queued`` tasks are leaseable (INV-EXEC-1). Expired tasks cycle back
    to ``queued`` before they can be re-leased, so this single-state predicate
    covers the whole lease-acquisition pre-condition.
    """
    return state in TASK_LEASEABLE_STATES


def task_is_completed(state: str) -> bool:
    """Return ``True`` iff a task is already in its terminal ``completed`` state."""
    return state == TASK_STATE_COMPLETED


def task_is_completable(state: str) -> bool:
    """Return ``True`` iff a worker may complete a task in *state*.

    Only a ``leased`` task may be completed (INV-EXEC-2); a task that is
    already ``completed`` cannot be completed again — the idempotency check in
    the service layer handles duplicates before state is inspected here.
    """
    return state in TASK_COMPLETABLE_STATES


def lease_is_active(
    lease_expires_at: datetime | None,
    now: datetime,
) -> bool:
    """Return ``True`` iff the lease has not yet expired at *now*.

    ``lease_expires_at`` is ``None`` for tasks that have never been leased;
    those are not ``leased`` in state either, so callers should not reach this
    predicate for un-leased tasks.
    """
    return lease_expires_at is not None and lease_expires_at > now


def lease_has_expired(
    state: str,
    lease_expires_at: datetime | None,
    now: datetime,
) -> bool:
    """Return ``True`` iff a leased task's lease has passed its expiry wall-clock.

    A task that is not in ``leased`` state (e.g., already completed or queued)
    cannot be *expired* regardless of the timestamp — only leased rows are
    candidates for the expiry reconciler.
    """
    return state == TASK_STATE_LEASED and not lease_is_active(lease_expires_at, now)


# --- DLQ governance predicates (INV-DLQ-2/3/4, doc 07 §4) ------------------


def dead_letter_accepts_proposal(state: str) -> bool:
    """Return ``True`` iff a dead-letter record may receive a correction proposal.

    INV-DLQ-2: the governed correction flow begins with attaching a diagnosis
    and proposal to an ``open`` dead-letter record, moving it to
    ``investigating``. A record already ``investigating`` or ``resolved`` cannot
    accept a new proposal without an explicit re-open (not modelled here).
    """
    return state == DEAD_LETTER_STATE_OPEN


def dead_letter_is_approvable(state: str, explicit_approval: bool) -> bool:
    """Return ``True`` iff a dead-letter record may be resolved.

    INV-DLQ-3: resolution requires *both* the record to be in the
    ``investigating`` state *and* an explicit approval signal from the caller.
    Auto-resolution (``explicit_approval=False``) is always rejected — the
    engine produces a diagnosis and proposed spec but never resolves the DLQ
    entry itself.
    """
    return state == DEAD_LETTER_STATE_INVESTIGATING and explicit_approval
