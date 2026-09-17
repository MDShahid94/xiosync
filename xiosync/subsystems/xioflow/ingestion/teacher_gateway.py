from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

class TeacherGateway:
    """Chrome extension POST handler."""

    def __init__(self, memory_graph):
        self.memory_graph = memory_graph

    async def record_action(
        self,
        *,
        org_id: str,
        url: str,
        intent: str,
        face_value: dict,
        place_value: dict,
        action_type: str,
        action_params: dict,
        previous_node_id: str | None = None,
        recorded_by: str | None = None,
        project_id: str | None = None
    ) -> str:
        """Record an action from the teacher extension."""
        parsed_url = urlparse(url)
        domain = parsed_url.netloc

        node_id = await self.memory_graph.save_new_action(
            org_id=org_id,
            domain=domain,
            url=url,
            intent=intent,
            face_value=face_value,
            place_value=place_value,
            action_type=action_type,
            action_params=action_params,
            previous_node_id=previous_node_id,
            recorded_by='teacher_extension',
            project_id=project_id,
            recording_method='teacher_extension'
        )
        return str(node_id)
