"""DAGGraphBuilder — converts a TraceCollector action stream into deployable DAG memory nodes.

Respects XIOSYNC ontology:
  - ``graph_class = GRAPH_CLASS_WORKFLOW`` (acyclic, doc 03 §3)
  - ``recording_method = 'auto_trace'``
  - ``tier = 'project_experimental'`` (will be promoted via ConsensusEngine)
  - ``context_hash`` via ``ContextHashRouter``

Output is a DAG JSON spec compatible with ``DAGDeployer.deploy_from_json()``.
"""

from __future__ import annotations

import logging
from typing import Any

from xiosync.subsystems.xioflow.engine.context_hash_router import ContextHashRouter

logger = logging.getLogger(__name__)


class DAGGraphBuilder:
    """Transforms a linear trace into a ``xioflow_memory_nodes`` DAG spec.

    The builder takes a ``TraceCollector`` with its ordered list of
    ``TraceAction`` objects and produces a JSON structure that can be
    deployed via ``DAGDeployer.deploy_from_json()`` or the
    ``POST /xioflow/dags/deploy`` API.

    Edges are sequential: each action links to the next via
    ``previous_intent`` / ``next_intents``.  Step boundaries are
    preserved as intent prefixes for readability.
    """

    # Action types that should NOT be included in the DAG
    _SKIP_ACTION_TYPES: frozenset[str] = frozenset(
        {
            "wait_for_selector",
            "wait_for_load_state",
        }
    )

    def build(
        self,
        trace: Any,  # TraceCollector
        context: dict[str, Any],  # Device/viewport context
        template_name: str = "",  # For template registration
    ) -> dict[str, Any]:
        """Build a deployable DAG JSON from a trace.

        Args:
            trace: ``TraceCollector`` instance with recorded actions.
            context: Device context dict with ``device_type``, ``os_name``,
                ``browser``, ``viewport_w``, ``viewport_h`` keys.
            template_name: Source template name for provenance tracking.

        Returns:
            dict compatible with ``DAGDeployer.deploy_from_json()``::

                {
                    "dag_domain": "...",
                    "dag_root_intent": "...",
                    "nodes": [...],
                    "metadata": {...},
                }
        """
        if not trace.actions:
            logger.info("dag_graph_builder: empty trace, nothing to build")
            return {
                "dag_domain": trace.domain,
                "dag_root_intent": "",
                "nodes": [],
            }

        # Filter out control-only actions
        meaningful = [a for a in trace.actions if a.action_type not in self._SKIP_ACTION_TYPES]

        if not meaningful:
            return {
                "dag_domain": trace.domain,
                "dag_root_intent": "",
                "nodes": [],
            }

        context_hash = ContextHashRouter.generate_context_hash(
            device_type=context.get("device_type", "desktop"),
            os_name=context.get("os_name", "linux"),
            browser=context.get("browser", "chrome"),
            viewport_w=context.get("viewport_w", 1920),
            viewport_h=context.get("viewport_h", 1080),
        )

        # Deduplicate intents to ensure uniqueness within the DAG
        seen_intents: dict[str, int] = {}
        for action in meaningful:
            base = action.intent
            if base in seen_intents:
                seen_intents[base] += 1
                action.intent = f"{base}_v{seen_intents[base]}"
            else:
                seen_intents[base] = 0

        nodes = []
        for i, action in enumerate(meaningful):
            # Build next_intents (linear chain)
            next_intents: list[str] = []
            if i < len(meaningful) - 1:
                next_intents = [meaningful[i + 1].intent]
            else:
                # Last action links to the terminal 'done' node
                done_intent = f"{action.step_name}__done"
                next_intents = [done_intent]

            # Determine domain from action URL or trace default
            domain = action.domain or trace.domain

            node: dict[str, Any] = {
                "domain": domain,
                "intent": action.intent,
                "action_type": self._map_action_type(action.action_type),
                "action_params": {
                    **action.action_params,
                    "next_intents": next_intents,
                },
                "face_value": action.face_value or {},
                "place_value": action.place_value or {},
                "previous_intent": meaningful[i - 1].intent if i > 0 else None,
                "context_hash": context_hash,
                "output_var": action.output_var,
                "execution_mode": "sequential",
                "recording_method": "auto_trace",
                "tier": "project_experimental",
                "volatility_type": self._volatility(action),
            }
            nodes.append(node)

        # Terminal 'done' node
        last = meaningful[-1]
        done_intent = f"{last.step_name}__done"
        nodes.append(
            {
                "domain": trace.domain,
                "intent": done_intent,
                "action_type": "done",
                "action_params": {"next_intents": []},
                "face_value": {},
                "place_value": {},
                "previous_intent": last.intent,
                "context_hash": context_hash,
                "recording_method": "auto_trace",
                "tier": "project_experimental",
            }
        )

        root_intent = nodes[0]["intent"]

        logger.info(
            "dag_graph_builder: built DAG with %d nodes (domain=%s, root=%s)",
            len(nodes),
            trace.domain,
            root_intent,
        )

        return {
            "dag_domain": trace.domain,
            "dag_root_intent": root_intent,
            "nodes": nodes,
            "metadata": {
                "source_template": template_name,
                "trace_summary": trace.to_summary(),
                "context_hash": context_hash,
                "recording_method": "auto_trace",
            },
        }

    def _map_action_type(self, action_type: str) -> str:
        """Map trace action types to valid xioflow_memory_node action types."""
        mapping = {
            "press": "fill",  # press is a fill variant
            "select_option": "fill",  # select is a fill variant
            "check": "click",  # check/uncheck are click variants
            "uncheck": "click",
            "hover": "click",  # hover is a click variant for DAG
            "extract_data": "extract_data",
        }
        return mapping.get(action_type, action_type)

    def _volatility(self, action: Any) -> str:
        """Determine volatility type for a traced action.

        Browser locators on dynamic pages are 'dynamic'; navigation
        and HTTP calls are 'static'.
        """
        if action.category == "browser" and action.action_type in ("click", "fill", "type"):
            return "dynamic"
        return "static"
