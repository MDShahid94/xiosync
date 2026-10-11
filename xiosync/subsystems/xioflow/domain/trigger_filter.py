"""Trigger filter expression evaluator.

Evaluates JSONPath-like filter expressions against event payloads.
Supports operators: eq, ne, gt, gte, lt, lte, in, contains, exists, and boolean combinations (all, any).
"""

from __future__ import annotations

from typing import Any


def _evaluate_filter(filter_expr: dict[str, Any] | None, event_payload: dict[str, Any]) -> bool:
    """Evaluate a filter expression against an event payload.

    Supports simple operators:
    - {"field": "amount", "op": "gt", "value": 1000}
    - {"field": "resource_type", "op": "eq", "value": "workflow"}
    - {"all": [filter1, filter2]}  (AND)
    - {"any": [filter1, filter2]}  (OR)
    """
    if filter_expr is None:
        return True  # no filter = always match

    if not isinstance(filter_expr, dict):
        return True

    if "all" in filter_expr:
        return all(_evaluate_filter(f, event_payload) for f in filter_expr["all"])
    if "any" in filter_expr:
        return any(_evaluate_filter(f, event_payload) for f in filter_expr["any"])

    field = filter_expr.get("field", "")
    op = filter_expr.get("op", "eq")
    expected = filter_expr.get("value")

    # Navigate nested fields with dot notation
    actual: Any = event_payload
    for part in field.split("."):
        if isinstance(actual, dict):
            actual = actual.get(part)
        else:
            actual = None
            break

    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    try:
        if op == "gt":
            return actual is not None and actual > expected
        if op == "gte":
            return actual is not None and actual >= expected
        if op == "lt":
            return actual is not None and actual < expected
        if op == "lte":
            return actual is not None and actual <= expected
    except TypeError:
        return False
    if op == "in":
        return actual in (expected or [])
    if op == "contains":
        return expected in (actual or [])
    if op == "exists":
        return actual is not None

    return True


evaluate_filter = _evaluate_filter

__all__ = ["_evaluate_filter", "evaluate_filter"]
