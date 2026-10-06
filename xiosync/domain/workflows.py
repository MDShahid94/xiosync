"""Workflow DAG domain logic — validation, cycle detection, and data flow.

Provides pure-Python (no I/O, no framework) utilities used by WorkflowService
to validate workflow specs before they are persisted or published.

Exported symbols
----------------
WorkflowCycleError      – raised when a spec contains a cycle
WorkflowSpecError       – raised when a spec is structurally invalid
DataFlowError           – raised when input_from wiring is invalid
validate_workflow_dag   – validate nodes/edges and data flow in one call
validate_data_flow      – validate only the data-flow (input_from) wiring
resolve_node_inputs     – runtime resolution of input_from → concrete dict
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WorkflowSpecError(ValueError):
    """A workflow spec is structurally invalid (e.g. dangling edge)."""


class WorkflowCycleError(ValueError):
    """A workflow spec contains a directed cycle."""


class DataFlowError(ValueError):
    """An ``input_from`` wiring declaration is invalid."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _topological_order(nodes: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """Return a topological ordering of nodes, or raise WorkflowCycleError."""
    in_degree: dict[str, int] = {n: 0 for n in nodes}
    adj: dict[str, list[str]] = defaultdict(list)

    for src, dst in edges:
        adj[src].append(dst)
        in_degree[dst] += 1

    queue: deque[str] = deque(n for n in nodes if in_degree[n] == 0)
    order: list[str] = []

    while queue:
        node = queue.popleft()
        order.append(node)
        for neighbour in adj[node]:
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    if len(order) != len(nodes):
        raise WorkflowCycleError(
            "workflow spec contains a directed cycle; topological sort failed"
        )
    return order


def _validate_input_from(
    node_id: str,
    input_from: Any,
    node_ids: set[str],
    topo_rank: dict[str, int],
    predecessors: dict[str, set[str]],
) -> None:
    """Validate a single node's ``input_from`` declaration."""
    if isinstance(input_from, str):
        upstream = input_from
        if upstream not in node_ids:
            raise DataFlowError(
                f"node {node_id!r} input_from references unknown node {upstream!r}"
            )
        if upstream not in predecessors[node_id]:
            raise DataFlowError(
                f"node {node_id!r} input_from {upstream!r}: "
                f"{upstream!r} is not a topological predecessor of {node_id!r}"
            )
    elif isinstance(input_from, dict):
        for key, source_path in input_from.items():
            if not key:
                raise DataFlowError(
                    f"node {node_id!r} input_from has an invalid key (empty string)"
                )
            if not isinstance(source_path, str) or not source_path:
                raise DataFlowError(
                    f"node {node_id!r} input_from[{key!r}] has an invalid source path"
                )
            upstream = source_path.split(".")[0]
            if upstream not in node_ids:
                raise DataFlowError(
                    f"node {node_id!r} input_from[{key!r}] references unknown node {upstream!r}"
                )
            if upstream not in predecessors[node_id]:
                raise DataFlowError(
                    f"node {node_id!r} input_from[{key!r}]: "
                    f"{upstream!r} is not a topological predecessor of {node_id!r}"
                )
    else:
        raise DataFlowError(
            f"node {node_id!r} input_from must be a string or mapping, "
            f"got {type(input_from).__name__!r}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_workflow_dag(spec: dict[str, Any]) -> None:
    """Validate a workflow spec dict for structural correctness and data flow.

    Raises
    ------
    WorkflowSpecError
        If an edge references a node that does not exist in ``spec["nodes"]``.
    WorkflowCycleError
        If the graph contains a directed cycle.
    DataFlowError
        If any node's ``input_from`` references an unknown or non-predecessor node.
    """
    raw_nodes: list[dict[str, Any]] = spec.get("nodes", [])
    raw_edges: list[dict[str, Any]] = spec.get("edges", [])

    node_ids: set[str] = {n["id"] for n in raw_nodes}

    edge_pairs: list[tuple[str, str]] = []
    for edge in raw_edges:
        src, dst = edge["from"], edge["to"]
        if src not in node_ids:
            raise WorkflowSpecError(f"edge references unknown source node: {src!r}")
        if dst not in node_ids:
            raise WorkflowSpecError(f"edge references unknown destination node: {dst!r}")
        edge_pairs.append((src, dst))

    topo_order = _topological_order(list(node_ids), edge_pairs)
    topo_rank: dict[str, int] = {n: i for i, n in enumerate(topo_order)}

    predecessors: dict[str, set[str]] = defaultdict(set)
    for src, dst in edge_pairs:
        predecessors[dst].add(src)
        predecessors[dst].update(predecessors[src])

    for node in raw_nodes:
        input_from = node.get("input_from")
        if input_from is None:
            continue
        _validate_input_from(node["id"], input_from, node_ids, topo_rank, predecessors)


def validate_data_flow(spec: dict[str, Any]) -> None:
    """Alias kept for backwards compatibility; delegates to validate_workflow_dag."""
    validate_workflow_dag(spec)


def resolve_node_inputs(
    input_from: str | dict[str, str],
    upstream_results: dict[str, Any],
) -> dict[str, Any]:
    """Resolve ``input_from`` declarations against actual upstream node results.

    Parameters
    ----------
    input_from:
        Either a string (whole-result shorthand: the upstream node id) or a
        key→source-path mapping like ``{"raw": "fetch.result.data"}``.
    upstream_results:
        Mapping of ``node_id → result`` for nodes that have already run.

    Returns
    -------
    dict
        Resolved kwargs dict for the next node.
    """
    if isinstance(input_from, str):
        upstream = upstream_results.get(input_from)
        if isinstance(upstream, dict):
            return upstream
        return {"_result": upstream}

    resolved: dict[str, Any] = {}
    for key, source_path in input_from.items():
        parts = source_path.split(".")
        node_id = parts[0]
        path = parts[1:]
        value: Any = upstream_results.get(node_id)
        for segment in path:
            if isinstance(value, dict):
                value = value.get(segment)
            else:
                value = None
                break
        resolved[key] = value
    return resolved
