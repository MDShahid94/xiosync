from __future__ import annotations

import json
import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)

class DAGDeployer:
    """JSON/YAML graph seeder."""

    def __init__(self, memory_graph):
        self.memory_graph = memory_graph

    async def deploy_from_json(self, graph_data: dict[str, Any], org_id: str, project_id: str | None = None, context_hash: str = 'default') -> list[str]:
        """Deploy a graph from JSON data."""
        nodes = graph_data.get('nodes', [])
        created_node_ids = []

        for node in nodes:
            # Extract domain from URL if provided, or use graph-level domain
            node_domain = node.get('domain', '')
            if not node_domain and node.get('url'):
                from urllib.parse import urlparse
                node_domain = urlparse(node['url']).netloc
            if not node_domain:
                node_domain = graph_data.get('domain', graph_data.get('dag_domain', ''))

            node_id = await self.memory_graph.asave_new_action(
                org_id=org_id,
                domain=node_domain,
                intent=node.get('intent'),
                face_value=node.get('face_value', {}),
                place_value=node.get('place_value', {}),
                action_type=node.get('action_type'),
                action_params=node.get('action_params', {}),
                previous_intent=node.get('previous_node_id'),
                recorded_by=None,            # UUID FK — not set for declarative import
                project_id=project_id,
                recording_method='declarative_dag',
                context_hash=context_hash,
                output_var=node.get('output_var'),
                execution_mode=node.get('execution_mode', 'sequential'),
            )
            created_node_ids.append(str(node_id))

        return created_node_ids

    def deploy_from_file(self, file_path: str, org_id: str, project_id: str | None = None) -> list[str]:
        """Deploy a graph from a JSON or YAML file."""
        with open(file_path, encoding='utf-8') as f:
            if file_path.endswith('.yaml') or file_path.endswith('.yml'):
                graph_data = yaml.safe_load(f)
            else:
                graph_data = json.load(f)

        import asyncio
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.deploy_from_json(graph_data, org_id, project_id))
