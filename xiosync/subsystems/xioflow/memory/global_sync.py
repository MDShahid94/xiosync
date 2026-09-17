from __future__ import annotations

import copy
import logging

from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph
from xiosync.subsystems.xioflow.memory.pii_scrubber import PIIScrubber

logger = logging.getLogger(__name__)

class GlobalSyncRouter:
    """Domain guard + global hivemind routing."""

    def __init__(self, memory_graph: MemoryGraph, pii_scrubber: PIIScrubber) -> None:
        """Initialize router."""
        self.memory_graph = memory_graph
        self.pii_scrubber = pii_scrubber

    def is_eligible_for_global(self, node: dict) -> bool:
        """Check if node is eligible for global sharing."""
        if node.get('tier') != 'organization_shared':
            return False
        if not self.pii_scrubber.is_public_domain(node.get('domain', '')):
            return False
        if node.get('status') != 'ACTIVE':
            return False
        return True

    def prepare_for_global(self, node: dict) -> dict:
        """Sanitize node for global tier."""
        sanitized = copy.deepcopy(node)

        if sanitized.get('place_value'):
            sanitized['place_value'] = self.pii_scrubber.redact_place_value(sanitized['place_value'])

        if sanitized.get('action_params'):
            scrubbed = self.pii_scrubber.scrub_dict(sanitized['action_params'])
            sanitized['action_params'] = {k: "" for k in scrubbed.keys()}

        if sanitized.get('face_value') and 'text' in sanitized['face_value']:
            sanitized['face_value']['text'] = ""

        sanitized['tier'] = 'platform_global'
        return sanitized
