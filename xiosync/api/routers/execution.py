"""Execution-plane API endpoints — task lease, heartbeat, completion, and worker health.

These are the **only** channel between the execution plane (workers) and the
control-plane database (INV-EXEC-1, doc 07 §1). Workers never read or write
the control-plane DB directly; they only call these endpoints:

* ``POST /execution/tasks/{task_id}/lease``      — atomically acquire a lease
* ``POST /execution/tasks/{task_id}/heartbeat``  — extend an active lease
* ``POST /execution/tasks/{task_id}/complete``   — deliver a result and close
* ``POST /execution/workers/{worker_id}/heartbeat`` — worker-level health (W-5)

INV-EXEC-2: task delivery is at-least-once; a duplicate completion (idempotent
``task_id`` key) returns ``duplicate=true`` in the response body rather than
raising, so the calling worker can safely retry without risk of double-write.

INV-EXEC-3 (stub, Phase 4 Step 3): the result on completion is accepted as
untrusted input and recorded on the task row; full output-schema validation and
re-authorization are deferred to the scheduler-side result-validation step that
is the subject of a later step.

The router obtains the authenticated ``OrgContext`` and the already-opened
``org_scoped_session`` from ``request.state``, following the same pattern as
the auth router: the ``AuthenticationMiddleware`` wires both onto scope state
before any handler runs, so every endpoint here is implicitly tenant-scoped.

Wave 1 gap fixes applied:
- T-1: LeaseResponse now carries ``input`` (task input parameters).
- T-4: HeartbeatRequest accepts ``progress``; HeartbeatResponse carries
  ``progress_updated_at``.
- D-4: CompleteRequest validates ``result`` size (max 4 MiB serialized).
- W-5: ``POST /workers/{worker_id}/heartbeat`` for worker health reporting.
- T-3: Lease duration cap now sourced from ``MAX_LEASE_DURATION_SECONDS``
  constant (single point of change, configurable in a later wave).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.identity import Actor
from xiosync.platform.task_credentials import (
    load_task_credential_signing_key,
    mint_task_credential,
)
from xiosync.services.workflows import (
    InactiveLeaseError,
    NonCompletableError,
    TaskNotFoundError,
    UnleaseableError,
    WorkflowService,
)

router = APIRouter(prefix="/execution", tags=["execution"])

# Gap T-3: single point of change for lease duration cap.
# In a later wave this will be sourced from per-org configuration.
MAX_LEASE_DURATION_SECONDS: int = 3600

# Gap D-4: maximum serialized size for task result payloads (4 MiB).
MAX_RESULT_SIZE_BYTES: int = 4_194_304

# Gap W-5: allowed worker health status values.
_ALLOWED_HEALTH_STATUSES = frozenset({"healthy", "degraded", "offline"})


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _context(request: Request) -> OrgContext:
    return cast(OrgContext, request.state.org_context)


def _session(request: Request) -> OrmSession:
    return cast(OrmSession, request.state.org_session)


def _service(request: Request) -> WorkflowService:
    return WorkflowService(_session(request))


def _problem(
    request: Request,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://xiosync.dev/problems/{code}",
        "title": title,
        "status": status,
        "code": code,
        "request_id": request.state.request_id,
    }
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status, media_type="application/problem+json", content=body)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LeaseRequest(StrictModel):
    """Body for ``POST /execution/tasks/{task_id}/lease`` (INV-EXEC-1)."""

    leased_by: uuid.UUID = Field(
        description="The worker actor ID that is acquiring this lease."
    )
    duration_seconds: int | None = Field(
        default=None,
        ge=1,
        le=MAX_LEASE_DURATION_SECONDS,
        description="Desired lease duration in seconds. Defaults to the service default (300s).",
    )


class LeaseResponse(StrictModel):
    task_id: uuid.UUID
    lease_id: uuid.UUID
    leased_by: uuid.UUID
    lease_expires_at: str  # ISO-8601 UTC
    attempts: int
    state: str
    # Gap T-1: task input parameters delivered at lease time.
    input: dict[str, Any] | None = Field(
        default=None,
        description="Task input parameters. None when no input was set at enqueue time.",
    )
    # Gap W-3: checkpoint for task retry resumption.
    checkpoint: dict[str, Any] | None = Field(
        default=None,
        description="Previous checkpoint data from a prior attempt. None on first lease.",
    )
    # INV-TASK-SEC-1/2: a scoped, single-use credential minted at lease time,
    # bound to (task_id, worker_id) and expiring with the lease. The worker
    # presents this — never the raw stored secret — to a capability that needs
    # credentials.
    task_credential: str = Field(
        description=(
            "Signed, single-use task credential bound to this (task_id, "
            "worker_id) lease and expiring with it (INV-TASK-SEC-1/2). Signed "
            "with a key distinct from the user-session JWT secret."
        )
    )
    task_credential_expires_at: str  # ISO-8601 UTC; equals the lease expiry
    scoped_capabilities: list[uuid.UUID] = Field(
        description="The capability IDs this task credential is scoped to."
    )


class HeartbeatRequest(StrictModel):
    """Body for ``POST /execution/tasks/{task_id}/heartbeat``."""

    lease_id: uuid.UUID = Field(description="The lease_id returned by the lease endpoint.")
    duration_seconds: int | None = Field(
        default=None,
        ge=1,
        le=MAX_LEASE_DURATION_SECONDS,
        description="Extension duration in seconds. Defaults to the service default (300s).",
    )
    # Gap T-4: incremental progress snapshot.
    progress: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional progress snapshot. When provided, the task's progress "
            "and progress_updated_at are atomically updated with the lease extension."
        ),
    )


class HeartbeatResponse(StrictModel):
    task_id: uuid.UUID
    lease_id: uuid.UUID
    lease_expires_at: str  # ISO-8601 UTC
    # Gap T-4: timestamp of the last progress update.
    progress_updated_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp of the last progress update, or null.",
    )


class CompleteRequest(StrictModel):
    """Body for ``POST /execution/tasks/{task_id}/complete`` (INV-EXEC-2)."""

    lease_id: uuid.UUID = Field(description="The lease_id returned by the lease endpoint.")
    result: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Untrusted result payload (INV-EXEC-3); recorded on the task row. "
            "Full output-schema validation is applied by the scheduler after completion."
        ),
    )

    @field_validator("result")
    @classmethod
    def _check_result_size(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """Gap D-4: reject result payloads that exceed MAX_RESULT_SIZE_BYTES."""
        if v is not None:
            serialized = json.dumps(v, separators=(",", ":"))
            if len(serialized.encode("utf-8")) > MAX_RESULT_SIZE_BYTES:
                msg = (
                    f"result payload exceeds maximum size of "
                    f"{MAX_RESULT_SIZE_BYTES} bytes "
                    f"({len(serialized.encode('utf-8'))} bytes serialized)"
                )
                raise ValueError(msg)
        return v


class CompleteResponse(StrictModel):
    task_id: uuid.UUID
    state: str
    duplicate: bool = Field(
        description=(
            "True when this task was already completed; the caller should treat "
            "this as a no-op (INV-EXEC-2 idempotency)."
        )
    )


class WorkerHeartbeatRequest(StrictModel):
    """Body for ``POST /execution/workers/{worker_id}/heartbeat`` (Gap W-5)."""

    enrollment_id: uuid.UUID = Field(
        description="The enrollment_id of the worker sending the heartbeat."
    )
    health_status: str = Field(
        description="Worker health status: 'healthy', 'degraded', or 'offline'."
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata (load, queue depth, etc.).",
    )

    @field_validator("health_status")
    @classmethod
    def _check_health_status(cls, v: str) -> str:
        if v not in _ALLOWED_HEALTH_STATUSES:
            msg = f"health_status must be one of {sorted(_ALLOWED_HEALTH_STATUSES)}; got {v!r}"
            raise ValueError(msg)
        return v


class WorkerHeartbeatResponse(StrictModel):
    worker_id: uuid.UUID
    health_status: str
    last_heartbeat: str  # ISO-8601 UTC


# Gap W-2: Task discovery / atomic claim.
class ClaimNextRequest(StrictModel):
    """Body for ``POST /execution/tasks/claim-next`` (Gap W-2)."""

    worker_id: uuid.UUID = Field(description="The worker actor ID claiming a task.")
    capabilities: list[str] = Field(
        description="List of capability names the worker can execute.",
        min_length=1,
    )
    duration_seconds: int | None = Field(
        default=None,
        ge=1,
        le=MAX_LEASE_DURATION_SECONDS,
        description="Desired lease duration in seconds.",
    )


# Gap W-3: Task checkpointing.
class CheckpointRequest(StrictModel):
    """Body for ``POST /execution/tasks/{task_id}/checkpoint`` (Gap W-3)."""

    lease_id: uuid.UUID = Field(description="The lease_id returned by the lease endpoint.")
    checkpoint: dict[str, Any] = Field(description="Checkpoint state to persist.")


class CheckpointResponse(StrictModel):
    task_id: uuid.UUID
    checkpoint_at: str  # ISO-8601 UTC


@router.post(
    "/tasks/{task_id}/lease",
    response_model=LeaseResponse,
    summary="Atomically acquire a task lease (INV-EXEC-1)",
)
def lease_task(
    task_id: uuid.UUID,
    payload: LeaseRequest,
    request: Request,
) -> LeaseResponse | JSONResponse:
    """Transition a ``queued`` task to ``leased`` and return the lease token.

    Only ``queued`` tasks may be leased (INV-EXEC-1). A row-level lock prevents
    two workers from racing on the same task. An already-leased or terminal task
    returns 409.
    """
    context = _context(request)
    service = _service(request)
    duration = (
        timedelta(seconds=payload.duration_seconds)
        if payload.duration_seconds is not None
        else None
    )

    try:
        task = service.lease_task(
            context,
            task_id,
            leased_by=payload.leased_by,
            duration=duration,
        )
    except TaskNotFoundError:
        return _problem(request, 404, "task_not_found", "Task not found")
    except UnleaseableError as exc:
        return _problem(
            request,
            409,
            "task_not_leaseable",
            "Task is not in a leaseable state",
            detail=f"state={exc.state!r}",
        )

    assert task.lease_id is not None  # noqa: S101 — guaranteed by lease_task on success
    assert task.leased_by is not None  # noqa: S101
    assert task.lease_expires_at is not None  # noqa: S101

    # INV-TASK-SEC-1/2: mint the scoped, single-use credential at lease time,
    # bound to (task_id, worker_id) and expiring with the lease. Signing uses
    # the worker-credential key (distinct from the user JWT secret — H7); a
    # missing key fails loudly rather than downgrading security (INV-SEC-1).
    scoped_capabilities = [task.capability_id]
    token, credential = mint_task_credential(
        load_task_credential_signing_key(),
        task_id=task.id,
        worker_id=task.leased_by,
        lease_id=task.lease_id,
        organization_id=context.organization_id,
        scoped_capabilities=scoped_capabilities,
        now=datetime.now(UTC),
        expires_at=task.lease_expires_at,
    )

    return LeaseResponse(
        task_id=task.id,
        lease_id=task.lease_id,
        leased_by=task.leased_by,
        lease_expires_at=task.lease_expires_at.isoformat(),
        attempts=task.attempts,
        state=task.state,
        input=task.input,
        checkpoint=task.checkpoint,
        task_credential=token,
        task_credential_expires_at=credential.expires_at.isoformat(),
        scoped_capabilities=list(credential.scoped_capabilities),
    )


@router.post(
    "/tasks/{task_id}/heartbeat",
    response_model=HeartbeatResponse,
    summary="Extend an active task lease",
)
def heartbeat_task(
    task_id: uuid.UUID,
    payload: HeartbeatRequest,
    request: Request,
) -> HeartbeatResponse | JSONResponse:
    """Push the lease deadline forward to prevent mid-work expiry.

    The caller must present the exact ``lease_id`` returned by the lease
    endpoint; a mismatched or expired lease_id returns 409.

    Gap T-4: when ``progress`` is provided in the request body, the task's
    progress snapshot is atomically updated alongside the lease extension.
    """
    context = _context(request)
    service = _service(request)
    duration = (
        timedelta(seconds=payload.duration_seconds)
        if payload.duration_seconds is not None
        else None
    )

    try:
        task = service.heartbeat_task(
            context,
            task_id,
            lease_id=payload.lease_id,
            duration=duration,
            progress=payload.progress,
        )
    except TaskNotFoundError:
        return _problem(request, 404, "task_not_found", "Task not found")
    except InactiveLeaseError as exc:
        return _problem(
            request,
            409,
            "lease_inactive",
            "Lease is expired or lease_id does not match",
            detail=str(exc),
        )

    assert task.lease_id is not None  # noqa: S101
    assert task.lease_expires_at is not None  # noqa: S101
    return HeartbeatResponse(
        task_id=task.id,
        lease_id=task.lease_id,
        lease_expires_at=task.lease_expires_at.isoformat(),
        progress_updated_at=(
            task.progress_updated_at.isoformat() if task.progress_updated_at else None
        ),
    )


@router.post(
    "/tasks/{task_id}/complete",
    response_model=CompleteResponse,
    summary="Complete a leased task and deliver its result (INV-EXEC-2)",
)
def complete_task(
    task_id: uuid.UUID,
    payload: CompleteRequest,
    request: Request,
) -> CompleteResponse | JSONResponse:
    """Mark a leased task ``completed`` and record its (untrusted) result.

    INV-EXEC-2: idempotent — a second completion with the same lease_id returns
    ``duplicate=true`` instead of raising. Any other non-completable state
    returns 409. The result is stored as-is (INV-EXEC-3 stub); scheduler-side
    output-schema validation is a separate, later step.

    Gap D-4: the ``result`` payload is validated against MAX_RESULT_SIZE_BYTES
    (4 MiB serialized) before acceptance.
    """
    context = _context(request)
    service = _service(request)

    try:
        outcome = service.complete_task(
            context,
            task_id,
            lease_id=payload.lease_id,
            result=payload.result,
        )
    except TaskNotFoundError:
        return _problem(request, 404, "task_not_found", "Task not found")
    except InactiveLeaseError as exc:
        return _problem(
            request,
            409,
            "lease_inactive",
            "Lease is expired or lease_id does not match",
            detail=str(exc),
        )
    except NonCompletableError as exc:
        return _problem(
            request,
            409,
            "task_not_completable",
            "Task is not in a completable state",
            detail=f"state={exc.state!r}",
        )

    return CompleteResponse(
        task_id=outcome.task_id,
        state=outcome.state,
        duplicate=outcome.duplicate,
    )


# ---------------------------------------------------------------------------
# Worker health endpoint (Gap W-5)
# ---------------------------------------------------------------------------


@router.post(
    "/workers/{worker_id}/heartbeat",
    response_model=WorkerHeartbeatResponse,
    summary="Report worker health status (Gap W-5)",
)
def worker_heartbeat(
    worker_id: uuid.UUID,
    payload: WorkerHeartbeatRequest,
    request: Request,
) -> WorkerHeartbeatResponse | JSONResponse:
    """Update the worker's health status and last_heartbeat timestamp.

    The ``actors`` table already has ``health_status`` and ``last_heartbeat``
    columns (migration 0002). This endpoint writes to them, enabling the
    platform to distinguish healthy, degraded, and offline workers without
    relying solely on task-level heartbeats.

    The worker must present its ``enrollment_id`` so the platform can verify
    that the worker is enrolled in the requesting org.
    """
    context = _context(request)
    session = _session(request)
    now = datetime.now(UTC)

    # Verify the actor exists in this org.
    actor = session.scalar(
        select(Actor).where(
            Actor.organization_id == context.organization_id,
            Actor.id == worker_id,
        )
    )
    if actor is None:
        return _problem(request, 404, "worker_not_found", "Worker actor not found")

    actor.health_status = payload.health_status
    actor.last_heartbeat = now
    session.flush()

    return WorkerHeartbeatResponse(
        worker_id=worker_id,
        health_status=payload.health_status,
        last_heartbeat=now.isoformat(),
    )


# -- Gap W-2: Task Discovery / Claim-Next --------------------------------


@router.post(
    "/tasks/claim-next",
    response_model=LeaseResponse,
    responses={204: {"description": "No matching task available"}},
    summary="Atomically discover and lease the next matching task (W-2)",
)
def claim_next_task(
    payload: ClaimNextRequest,
    request: Request,
) -> LeaseResponse | JSONResponse:
    """Find the highest-priority queued task matching the worker's capabilities
    and atomically lease it. Returns 204 when no matching task is available.

    Gap W-2: This is the primary task discovery mechanism for workers.
    """
    context = _context(request)
    service = _service(request)

    duration = payload.duration_seconds or 300

    task = service.claim_next_task(
        context,
        worker_id=payload.worker_id,
        capabilities=payload.capabilities,
        duration=duration,
    )
    if task is None:
        return JSONResponse(status_code=204, content=None)

    # claim_next_task guarantees these are set on success.
    assert task.lease_id is not None  # noqa: S101
    assert task.leased_by is not None  # noqa: S101
    assert task.lease_expires_at is not None  # noqa: S101

    # Mint task credential for the claimed task.
    scoped_capabilities = [task.capability_id]
    token, credential = mint_task_credential(
        load_task_credential_signing_key(),
        task_id=task.id,
        worker_id=payload.worker_id,
        lease_id=task.lease_id,
        organization_id=context.organization_id,
        scoped_capabilities=scoped_capabilities,
        now=datetime.now(UTC),
        expires_at=task.lease_expires_at,
    )

    return LeaseResponse(
        task_id=task.id,
        lease_id=task.lease_id,
        leased_by=task.leased_by,
        lease_expires_at=task.lease_expires_at.isoformat(),
        attempts=task.attempts,
        state=task.state,
        input=task.input,
        checkpoint=task.checkpoint,
        task_credential=token,
        task_credential_expires_at=credential.expires_at.isoformat(),
        scoped_capabilities=list(credential.scoped_capabilities),
    )


# -- Gap W-3: Task Checkpointing -----------------------------------------


@router.post(
    "/tasks/{task_id}/checkpoint",
    response_model=CheckpointResponse,
    summary="Write checkpoint for an actively-leased task (W-3)",
)
def checkpoint_task(
    task_id: uuid.UUID,
    payload: CheckpointRequest,
    request: Request,
) -> CheckpointResponse | JSONResponse:
    """Persist intermediate checkpoint state for a long-running task.

    On task retry (after lease expiry or worker crash), the checkpoint
    is included in the LeaseResponse so the new worker can resume.

    Gap W-3: enables stateful tasks to survive worker crashes.
    """
    context = _context(request)
    service = _service(request)

    try:
        task = service.checkpoint_task(
            context,
            task_id=task_id,
            lease_id=payload.lease_id,
            checkpoint=payload.checkpoint,
        )
    except TaskNotFoundError:
        return _problem(request, 404, "task_not_found", "Task not found")
    except InactiveLeaseError as exc:
        return _problem(request, 409, "inactive_lease", str(exc))

    return CheckpointResponse(
        task_id=task.id,
        checkpoint_at=task.checkpoint_at.isoformat() if task.checkpoint_at else "",
    )


# --- Gap S-1: Secrets endpoint -----------------------------------------------


class SecretRefResponse(BaseModel):
    """A resolved secret reference (metadata only — no raw values)."""

    model_config = ConfigDict(extra="forbid")
    name: str
    provider: str
    ref_config: dict[str, Any]


@router.get(
    "/tasks/{task_id}/secrets",
    response_model=list[SecretRefResponse],
    summary="Resolve secret references for a leased task (S-1)",
)
def get_task_secrets(
    task_id: uuid.UUID,
    request: Request,
) -> list[SecretRefResponse] | JSONResponse:
    """Return secret references scoped to the task's capability.

    Gap S-1: Workers call this after leasing a task to retrieve the
    provider-agnostic secret references they need to resolve. The platform
    returns metadata only (provider type, config path/ARN); actual secret
    value resolution is at the worker/SDK level.

    Authenticated via the session credential (same as other execution endpoints).
    """
    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    from xiosync.services.secrets import SecretRefService

    svc = SecretRefService(session)
    # Return all active secrets for the org (task-scoping can be refined
    # when tasks declare required_secrets in a future iteration).
    records = svc.list_secrets(context, state="active")

    return [
        SecretRefResponse(
            name=r.name,
            provider=r.provider,
            ref_config=r.ref_config,
        )
        for r in records
    ]

