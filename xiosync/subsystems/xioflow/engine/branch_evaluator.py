from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

class BranchEvaluator:
    """LLM-assisted conditional branching."""

    async def evaluate_branch(self, next_nodes: list[dict], workflow_vars: dict) -> str | None:
        """Evaluate conditions and return next node."""
        if not next_nodes:
            return None

        if len(next_nodes) == 1:
            return next_nodes[0].get('id')

        # Try simple string matching
        for node in next_nodes:
            condition = node.get("condition")
            if condition and isinstance(condition, dict):
                var_name = condition.get("var")
                expected = condition.get("val")
                if var_name in workflow_vars and str(workflow_vars[var_name]) == str(expected):
                    return node.get('id')

        # Fallback to first node
        logger.info("branch_fallback_first", nodes=len(next_nodes))
        return next_nodes[0].get('id')
