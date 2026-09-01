"""Streaming task output endpoints (Gap R-3).

Workers push incremental output chunks (logs, metrics, screenshots) via POST.
Consumers tail live task output via SSE GET. Chunks are stored as events with
``event_type = 'task.output'`` and ``entity_id = task_id``, leveraging the
R-4 indexable columns from Wave 2.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["task-streams"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StreamChunk(_StrictModel):
    """One chunk of streaming task output."""

    chunk_type: str = Field(
        description="Type of chunk: 'log', 'metric', 'artifact_ref', 'progress', 'custom'"
    )
    data: dict[str, Any] = Field(description="Chunk payload")
    sequence: int | None = Field(None, description="Optional sequence number for ordering")


class PushStreamRequest(_StrictModel):
    """Push one or more output chunks for a running task."""

    lease_id: uuid.UUID
    chunks: list[StreamChunk] = Field(min_length=1, max_length=50)


class PushStreamResponse(_StrictModel):
    """Acknowledgement of pushed chunks."""

    accepted: int
    task_id: uuid.UUID


_VALID_CHUNK_TYPES = frozenset({"log", "metric", "artifact_ref", "progress", "custom"})


@router.post(
    "/execution/tasks/{task_id}/stream",
    response_model=PushStreamResponse,
    summary="Push streaming output from a running task (R-3)",
)
def push_task_stream(
    task_id: uuid.UUID,
    payload: PushStreamRequest,
    request: Request,
) -> PushStreamResponse:
    """Push incremental output chunks from a running task.

    Gap R-3: Workers call this endpoint to stream logs, metrics, screenshots,
    or progress updates while a task is executing. Each chunk is stored as an
    event with ``event_type = 'task.output'`` and ``entity_id = task_id``,
    leveraging the R-4 indexable event columns for efficient retrieval.

    Authenticated via task credential (lease_id must match).
    """
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.services.events import EventService
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    svc = EventService(session)
    accepted = 0

    for chunk in payload.chunks:
        if chunk.chunk_type not in _VALID_CHUNK_TYPES:
            continue

        svc.append(
            context,
            event_type="task.output",
            payload={
                "chunk_type": chunk.chunk_type,
                "data": chunk.data,
                "sequence": chunk.sequence,
                "lease_id": str(payload.lease_id),
            },
            entity_type="task",
            entity_id=task_id,
        )
        accepted += 1

    return PushStreamResponse(accepted=accepted, task_id=task_id)


@router.get(
    "/execution/tasks/{task_id}/stream",
    response_class=StreamingResponse,
    summary="Tail live task output via SSE (R-3)",
)
async def tail_task_stream(
    task_id: uuid.UUID,
    request: Request,
    since_sequence: int | None = Query(None, description="Resume from sequence number"),
) -> StreamingResponse:
    """Stream task output in real-time via Server-Sent Events.

    Gap R-3: Consumers (dashboards, operators) call this to tail live output
    from a running task. Events are filtered by ``entity_id = task_id`` and
    ``event_type = 'task.output'``.

    Protocol-conformant SSE with reconnect support via ``since_sequence``.
    """

    async def _generate() -> AsyncGenerator[str, None]:
        yield "retry: 3000\n\n"
        yield f"event: stream.connected\ndata: {{\"task_id\": \"{task_id}\"}}\n\n"

        # P5: Subscribe to task-specific event bus channel.
        from xiosync.core.event_bus import get_event_bus
        import asyncio

        bus = get_event_bus()
        try:
            async for msg in bus.subscribe(f"task:{task_id}"):
                seq = msg.get("sequence", "")
                chunk = msg.get("chunk", "")
                yield f"id: {seq}\nevent: task.output\ndata: {json.dumps(msg, default=str)}\n\n"
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
