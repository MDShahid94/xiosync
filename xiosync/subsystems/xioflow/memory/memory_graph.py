from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from xiosync.subsystems.xioflow.models.memory_nodes import XioflowMemoryNode

logger = logging.getLogger(__name__)

class MemoryGraph:
    """CRUD operations for xioflow_memory_nodes."""

    def __init__(self, session: Session) -> None:
        """Initialize with a SQLAlchemy session."""
        self.session = session

    def save_new_action(
        self,
        *,
        org_id: str,
        domain: str,
        intent: str,
        face_value: dict | None,
        place_value: dict | None,
        action_type: str,
        action_params: dict | None,
        context_hash: str = 'default',
        device_type: str | None = None,
        os_name: str | None = None,
        browser: str | None = None,
        viewport_width: int | None = None,
        viewport_height: int | None = None,
        previous_intent: str | None = None,
        recording_method: str = 'auto_learn',
        recorded_by: str | None = None,
        client_id: str | None = None,
        volatility_type: str = 'static',
        fallback_plugin: str | None = None,
        output_var: str | None = None,
        execution_mode: str = 'sequential',
        project_id: str | None = None
    ) -> uuid.UUID:
        """Creates a new XioflowMemoryNode and returns its ID."""
        node = XioflowMemoryNode(
            id=uuid.uuid4(),
            org_id=org_id,
            domain=domain,
            intent=intent,
            face_value=face_value,
            place_value=place_value,
            action_type=action_type,
            action_params=action_params,
            context_hash=context_hash,
            device_type=device_type,
            os_name=os_name,
            browser=browser,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            previous_intent=previous_intent,
            recording_method=recording_method,
            recorded_by=recorded_by,
            client_id=client_id,
            volatility_type=volatility_type,
            fallback_plugin=fallback_plugin,
            output_var=output_var,
            execution_mode=execution_mode,
            project_id=project_id,
            tier="project_experimental",
            status="ACTIVE"
        )
        self.session.add(node)
        self.session.commit()
        return node.id

    def get_node(self, node_id: uuid.UUID) -> dict | None:
        """Get a node by ID."""
        node = self.session.query(XioflowMemoryNode).filter_by(id=node_id).first()
        if not node:
            return None
        return node.__dict__

    def update_node(self, node_id: uuid.UUID, **kwargs) -> None:
        """Update a node's properties."""
        node = self.session.query(XioflowMemoryNode).filter_by(id=node_id).first()
        if node:
            for key, value in kwargs.items():
                setattr(node, key, value)
            self.session.commit()

    def lookup_action(
        self,
        domain: str,
        intent: str,
        context_hash: str,
        org_id: str,
        tiers: list[str] | None = None
    ) -> dict | None:
        """Queries by lookup_key and tier priority order."""
        if not tiers:
            tiers = ["project_experimental", "project_ground_truth", "organization_shared", "platform_global"]

        query = self.session.query(XioflowMemoryNode).filter_by(
            domain=domain, intent=intent, context_hash=context_hash, org_id=org_id, status="ACTIVE"
        ).all()

        nodes_by_tier = {n.tier: n for n in query}
        for tier in tiers:
            if tier in nodes_by_tier:
                return nodes_by_tier[tier].__dict__

        return None

    def get_workflow_graph(
        self,
        domain: str,
        start_intent: str,
        org_id: str,
        context: dict,
        max_fallback_tier: int = 5,
        max_depth: int = 100,
    ) -> dict | None:
        """Recursively traverses next_nodes to build a full DAG dict.

        Each node's action_params may contain a ``next_intents`` list of intent
        strings that define outbound edges.  The resulting dict mirrors the
        structure consumed by ``DAGExecutor._execute_node()``.
        """
        visited: set[str] = set()

        def traverse(intent: str, current_depth: int) -> dict | None:
            if current_depth > max_depth or intent in visited:
                return None

            visited.add(intent)

            node = self.lookup_action(
                domain, intent, context.get("context_hash", "default"), org_id
            )
            if not node:
                return None

            result: dict = {
                "id": str(node["id"]),
                "intent": intent,
                "action_type": node.get("action_type"),
                "action_params": node.get("action_params") or {},
                "face_value": node.get("face_value"),
                "place_value": node.get("place_value"),
                "output_var": node.get("output_var"),
                "execution_mode": node.get("execution_mode", "sequential"),
                "next_nodes": [],
            }

            # next_intents is a list of intent strings stored in action_params
            # e.g. {"next_intents": ["fill_password", "submit_form"]}
            next_intents = (node.get("action_params") or {}).get("next_intents", [])
            for next_intent in next_intents:
                child = traverse(next_intent, current_depth + 1)
                if child is not None:
                    result["next_nodes"].append(child)

            return result

        return traverse(start_intent, 0)


    def update_locator_priority(self, node_id: uuid.UUID, new_priority: list[int]) -> None:
        """Update locator priority array."""
        self.update_node(node_id, locator_priority=new_priority)

    def update_last_used(self, node_id: uuid.UUID) -> None:
        """Update last_used timestamp."""
        self.update_node(node_id, last_used_at=datetime.utcnow())
