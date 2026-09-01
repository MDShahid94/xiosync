"""Pluggable event bus for real-time pub/sub (Gap R-1/R-3).

Two built-in backends:
- ``InProcessEventBus``: asyncio.Queue-based, single-process (dev/testing)
- ``RedisEventBus``: Redis Pub/Sub, multi-process (production)

The backend is selected via the ``EVENT_BUS_BACKEND`` environment variable
(default: ``"inprocess"``). This keeps the platform universal — deployers
choose their infrastructure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger("xiosync.core.event_bus")

__all__ = [
    "InProcessEventBus",
    "RedisEventBus",
    "get_event_bus",
]


class InProcessEventBus:
    """In-process event bus using asyncio — for development and testing.

    Messages are lost on process restart. Suitable for single-node deployments
    where SSE consumers and event producers share the same process.
    """

    def __init__(self) -> None:
        self._channels: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        for q in self._channels.get(channel, []):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(
                    "event_bus_queue_full",
                    extra={"channel": channel},
                )

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._channels[channel].append(q)
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            self._channels[channel].remove(q)

    async def close(self) -> None:
        self._channels.clear()


class RedisEventBus:
    """Redis Pub/Sub event bus — for production multi-process deployments.

    Requires ``REDIS_URL`` to be set. Uses Redis Pub/Sub channels so that
    multiple API server processes can fan out SSE events.
    """

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis
        self._redis: Any = aioredis.from_url(redis_url, decode_responses=True)  # type: ignore[no-untyped-call]

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        payload = json.dumps(message, default=str)
        await self._redis.publish(channel, payload)

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw_msg in pubsub.listen():
                if raw_msg["type"] == "message":
                    try:
                        yield json.loads(raw_msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    async def close(self) -> None:
        await self._redis.close()


# -- Singleton factory -------------------------------------------------------

_bus: InProcessEventBus | RedisEventBus | None = None


def get_event_bus() -> InProcessEventBus | RedisEventBus:
    """Get or create the singleton event bus based on ``EVENT_BUS_BACKEND``.

    Supported values:
    - ``"inprocess"`` (default): In-process asyncio queues
    - ``"redis"``: Redis Pub/Sub (requires ``REDIS_URL``)
    """
    global _bus
    if _bus is not None:
        return _bus

    backend = os.environ.get("EVENT_BUS_BACKEND", "inprocess").lower()
    if backend == "redis":
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _bus = RedisEventBus(redis_url)
        logger.info("event_bus_initialized", extra={"backend": "redis"})
    else:
        _bus = InProcessEventBus()
        logger.info("event_bus_initialized", extra={"backend": "inprocess"})

    return _bus
