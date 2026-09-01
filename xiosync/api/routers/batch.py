"""Batch / bulk operation endpoints (Gap P-3).

Provides ``POST /api/v1/batch`` for submitting multiple operations in a
single HTTP request. Each item is processed independently (partial success
is possible), and results are returned in order with per-item status codes.

The batch envelope is deliberately simple: an array of ``{action, payload}``
items. This avoids imposing a specific batch protocol while keeping the
surface universal enough to wrap any existing endpoint.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["batch"])


class BatchItem(BaseModel):
    """One item in a batch request."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        description=(
            "The operation to perform. Supported actions: "
            "'enqueue_task', 'create_artifact', 'append_event', "
            "'create_capability'."
        )
    )
    payload: dict[str, Any] = Field(description="Action-specific payload.")


class BatchRequest(BaseModel):
    """Batch request envelope."""

    model_config = ConfigDict(extra="forbid")

    items: list[BatchItem] = Field(
        description="Ordered list of operations to execute.",
        min_length=1,
        max_length=100,
    )


class BatchItemResult(BaseModel):
    """Result of one batch item."""

    index: int
    status: int
    action: str
    result: dict[str, Any] | None = None
    error: str | None = None


class BatchResponse(BaseModel):
    """Batch response envelope."""

    total: int
    succeeded: int
    failed: int
    results: list[BatchItemResult]


@router.post(
    "/batch",
    response_model=BatchResponse,
    summary="Execute multiple operations in a single request (P-3)",
)
def batch_operations(
    payload: BatchRequest,
    request: Request,
) -> BatchResponse:
    """Process a batch of independent operations.

    Gap P-3: Each item is processed independently within the caller's
    transaction. Partial success is possible — individual items may fail
    without affecting others. Results are returned in input order.

    Supported actions:
    - ``enqueue_task``: Enqueue a task (requires run_id, node_id, capability_id)
    - ``create_artifact``: Create an artifact reference
    - ``append_event``: Append an event to the audit stream
    - ``create_capability``: Register a new capability

    Unknown actions return a 400 status for that item.
    """
    results: list[BatchItemResult] = []
    succeeded = 0
    failed = 0

    for idx, item in enumerate(payload.items):
        try:
            result = _dispatch_action(request, item.action, item.payload)
            results.append(
                BatchItemResult(
                    index=idx,
                    status=200,
                    action=item.action,
                    result=result,
                )
            )
            succeeded += 1
        except ValueError as exc:
            results.append(
                BatchItemResult(
                    index=idx,
                    status=400,
                    action=item.action,
                    error=str(exc),
                )
            )
            failed += 1
        except Exception as exc:
            results.append(
                BatchItemResult(
                    index=idx,
                    status=500,
                    action=item.action,
                    error=f"internal error: {type(exc).__name__}",
                )
            )
            failed += 1

    return BatchResponse(
        total=len(payload.items),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )


def _dispatch_action(
    request: Request,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Route a batch action to the appropriate service method.

    Returns a result dict on success, raises on failure.
    """
    from xiosync.domain.context import OrgContext
    from sqlalchemy.orm import Session as OrmSession
    from typing import cast

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    if action == "enqueue_task":
        from xiosync.services.workflows import WorkflowService
        wf_svc = WorkflowService(session)
        task_id = wf_svc.enqueue_task(
            context,
            run_id=uuid.UUID(payload["run_id"]),
            node_id=payload["node_id"],
            capability_id=uuid.UUID(payload["capability_id"]),
            input=payload.get("input"),
            priority=payload.get("priority", 5),
        )
        return {"task_id": str(task_id), "state": "queued"}

    elif action == "create_artifact":
        from xiosync.services.artifacts import ArtifactService
        art_svc = ArtifactService(session)
        artifact = art_svc.create_artifact(
            context,
            provider_type=payload["provider_type"],
            uri=payload["uri"],
            created_by=uuid.UUID(payload["created_by"]),
            content_type=payload.get("content_type"),
            size_bytes=payload.get("size_bytes"),
            checksum=payload.get("checksum"),
            metadata=payload.get("metadata"),
        )
        return {"artifact_id": str(artifact.id)}

    elif action == "append_event":
        from xiosync.services.events import EventService
        evt_svc = EventService(session)
        event_id = evt_svc.append(
            context,
            event_type=payload["event_type"],
            payload=payload.get("payload", {}),
            actor_id=uuid.UUID(payload["actor_id"]) if payload.get("actor_id") else None,
            severity=payload.get("severity"),
        )
        return {"event_id": str(event_id)}

    elif action == "create_capability":
        from xiosync.services.capabilities import CapabilityService
        cap_svc = CapabilityService(session)
        cap = cap_svc.create_capability(
            context,
            name=payload["name"],
            description=payload.get("description"),
            input_schema=payload.get("input_schema"),
            output_schema=payload.get("output_schema"),
            execution_mode=payload.get("execution_mode", "sync"),
            timeout_ms=payload.get("timeout_ms"),
            retry_policy=payload.get("retry_policy"),
            state=payload.get("state", "active"),
        )
        return {"capability_id": str(cap.id), "name": cap.name}

    else:
        raise ValueError(f"unknown batch action: {action!r}")
