from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

class SSEEmitter:
    """Server-Sent Events emitter using in-memory pub-sub."""

    CHANNELS = ['system', 'workers', 'memory', 'workflows', 'dlq']

    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = {
            channel: set() for channel in self.CHANNELS
        }

    def subscribe(self, channel: str) -> asyncio.Queue:
        """Subscribe to a channel."""
        if channel not in self.CHANNELS:
            raise ValueError(f"Channel {channel} is not supported")

        queue = asyncio.Queue()
        self._subscribers[channel].add(queue)
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        """Unsubscribe from a channel."""
        if channel in self._subscribers and queue in self._subscribers[channel]:
            self._subscribers[channel].remove(queue)

    def emit(self, channel: str, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event to all subscribers on a channel."""
        if channel not in self._subscribers:
            return

        message = {
            'event': event_type,
            'data': data
        }

        for queue in list(self._subscribers[channel]):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(f"Queue full for subscriber on channel {channel}")
