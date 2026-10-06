"""Trace-specific SSE event types for real-time observability.

When trace_mode is active, these events are emitted alongside the
normal workflow execution events so XIOVIEW and the admin dashboard
can show live trace capture progress.
"""
from __future__ import annotations

from typing import Any


def trace_started(run_id: str, domain: str) -> dict[str, Any]:
    """Emitted when auto-trace begins on a workflow run."""
    return {
        "type": "trace.started",
        "run_id": run_id,
        "domain": domain,
    }


def trace_action_captured(
    run_id: str,
    action_type: str,
    step_name: str,
    intent: str,
    category: str,
    has_locators: bool,
) -> dict[str, Any]:
    """Emitted each time PageProxy captures an action."""
    return {
        "type": "trace.action_captured",
        "run_id": run_id,
        "action_type": action_type,
        "step_name": step_name,
        "intent": intent,
        "category": category,
        "has_locators": has_locators,
    }


def trace_dag_built(
    run_id: str,
    node_count: int,
    dag_domain: str,
    dag_root_intent: str,
) -> dict[str, Any]:
    """Emitted when DAGGraphBuilder finishes building the DAG."""
    return {
        "type": "trace.dag_built",
        "run_id": run_id,
        "node_count": node_count,
        "dag_domain": dag_domain,
        "dag_root_intent": dag_root_intent,
    }


def trace_deployed(
    run_id: str,
    node_count: int,
    template_name: str,
) -> dict[str, Any]:
    """Emitted when the traced DAG is deployed to memory nodes."""
    return {
        "type": "trace.deployed",
        "run_id": run_id,
        "node_count": node_count,
        "template_name": template_name,
    }


def trace_failed(run_id: str, error: str) -> dict[str, Any]:
    """Emitted when trace capture or deploy fails (non-fatal)."""
    return {
        "type": "trace.failed",
        "run_id": run_id,
        "error": error,
    }
