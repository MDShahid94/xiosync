from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

class WSConnectionManager:
    """WebSocket connection manager."""

    def __init__(self):
        # Using a generic type for WebSocket to avoid external dependencies
        self._connections: dict[str, list[Any]] = {}

    async def connect(self, websocket: Any, channels: list[str]) -> None:
        """Accept a websocket and register to channels."""
        await websocket.accept()
        for channel in channels:
            if channel not in self._connections:
                self._connections[channel] = []
            self._connections[channel].append(websocket)

    async def disconnect(self, websocket: Any) -> None:
        """Remove a websocket from all channels."""
        for channel_connections in self._connections.values():
            if websocket in channel_connections:
                channel_connections.remove(websocket)

    async def broadcast(self, channel: str, message: dict[str, Any]) -> None:
        """Broadcast a message to all connections on a channel."""
        if channel in self._connections:
            for connection in self._connections[channel]:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    logger.error(f"Error broadcasting to connection on {channel}: {e}")

    async def send_personal(self, websocket: Any, message: dict[str, Any]) -> None:
        """Send a message to a specific connection."""
        try:
            await websocket.send_json(message)
        except Exception as e:
            logger.error(f"Error sending personal message: {e}")
