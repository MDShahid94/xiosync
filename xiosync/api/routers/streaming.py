"""Server-Sent Events (SSE) endpoint for real-time event streaming (Gap R-1).

Provides ``GET /api/v1/events/stream`` for SSE-based real-time event
consumption. Supports both Bearer token and query-param ``?token=`` auth
(configurable per-org). The stream delivers events as they are created,
filtered by event_type and/or severity.

This is a stub implementation that demonstrates the SSE protocol:
- ``text/event-stream`` content type
- ``data:`` lines with JSON-encoded events
- ``id:`` lines for Last-Event-ID reconnection
- ``retry:`` hint for reconnect backoff
- ``event:`` field matching the event_type

Full production implementation requires a pub/sub backend (Redis, Kafka, etc.)
which is an org-level infrastructure choice. This stub polls the DB.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["streaming"])


async def _sse_event(
    *,
    event_id: str,
    event_type: str,
    data: dict[str, Any],
) -> str:
    """Format a single SSE message."""
    lines = [
        f"id: {event_id}",
        f"event: {event_type}",
        f"data: {json.dumps(data, default=str)}",
        "",  # blank line terminates the event
    ]
    return "\n".join(lines) + "\n"


@router.get(
    "/events/stream",
    summary="Real-time SSE event stream (R-1)",
    response_class=StreamingResponse,
)
async def event_stream(
    request: Request,
    event_type: str | None = Query(None, description="Filter by event type"),
    severity: str | None = Query(None, description="Filter by severity"),
    since_id: str | None = Query(None, description="Last-Event-ID for reconnection"),
) -> StreamingResponse:
    """Stream events in real-time via Server-Sent Events.

    Gap R-1: Supports both ``Authorization: Bearer <token>`` and ``?token=``
    query parameter authentication (configurable per-org setting).

    The stream sends:
    - ``retry: 5000`` on connect (5s reconnect backoff)
    - Each event as ``event: <event_type>\\ndata: <json>\\nid: <event_id>``

    **Stub note:** This implementation returns a protocol-conformant SSE
    response with the initial retry hint and connection keepalive. Full
    production streaming requires a pub/sub transport layer.
    """

    async def _generate() -> AsyncGenerator[str, None]:
        # Initial retry hint (milliseconds).
        yield "retry: 5000\n\n"

        # Connection established event.
        yield await _sse_event(
            event_id="0",
            event_type="connection.established",
            data={
                "message": "SSE stream connected",
                "filters": {
                    "event_type": event_type,
                    "severity": severity,
                    "since_id": since_id,
                },
            },
        )

        # P5: Subscribe to the org-scoped event bus channel.
        from xiosync.core.event_bus import get_event_bus
        import asyncio

        org_id = str(getattr(request.state, "organization_id", "unknown"))
        bus = get_event_bus()
        try:
            async for msg in bus.subscribe(f"events:{org_id}"):
                # Apply filters.
                msg_type = msg.get("event_type", "")
                msg_severity = msg.get("severity", "")
                if event_type and msg_type != event_type:
                    continue
                if severity and msg_severity != severity:
                    continue
                yield await _sse_event(
                    event_id=msg.get("event_id", ""),
                    event_type=msg_type,
                    data=msg,
                )
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

from xiosync.api.router_registry import register_router
from xiosync.api.middleware.rbac import require_capability
register_router(
    router,
    prefix='/api/v1',
    tags=["streaming"],
    dependencies=[require_capability("event.manage")],
)
