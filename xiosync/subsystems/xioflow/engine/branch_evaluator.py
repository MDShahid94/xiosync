from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class BranchEvaluator:
    """LLM-assisted conditional branching."""

    async def evaluate(
        self, condition: dict | str | None, execution_context: dict, workflow_vars: dict
    ) -> bool:
        """Evaluate a single edge condition. Returns True if the edge should be taken.

        Condition formats:
          - dict: {"var": "status", "val": "success"} — matches workflow_vars[var] == val
          - dict: {"var": "status", "op": "ne", "val": "error"} — with operator
          - str: "default" — always True (unconditional edge)
          - None: always True
        """
        if condition is None or condition == "default":
            return True
        if isinstance(condition, str):
            return True  # named conditions without dict structure are pass-through
        if isinstance(condition, dict):
            var_name = condition.get("var")
            expected = condition.get("val")
            op = condition.get("op", "eq")
            if var_name is None:
                return True
            actual = workflow_vars.get(var_name, execution_context.get(var_name))
            if op == "eq":
                return str(actual) == str(expected)
            elif op == "ne":
                return str(actual) != str(expected)
            elif op == "in":
                return str(actual) in (expected if isinstance(expected, list) else [expected])
            elif op == "gt":
                try:
                    return float(actual) > float(expected)
                except (TypeError, ValueError):
                    return False
            elif op == "lt":
                try:
                    return float(actual) < float(expected)
                except (TypeError, ValueError):
                    return False
            return str(actual) == str(expected)
        return True

    async def evaluate_branch(self, next_nodes: list[dict], workflow_vars: dict) -> str | None:
        """Evaluate conditions and return next node."""
        if not next_nodes:
            return None

        if len(next_nodes) == 1:
            return next_nodes[0].get("id")

        # Try simple string matching
        for node in next_nodes:
            condition = node.get("condition")
            if condition and isinstance(condition, dict):
                var_name = condition.get("var")
                expected = condition.get("val")
                if var_name in workflow_vars and str(workflow_vars[var_name]) == str(expected):
                    return node.get("id")

        # Fallback to first node
        logger.info("branch_fallback_first: %d candidates, picking first", len(next_nodes))
        return next_nodes[0].get("id")
