"""DLQ governance API endpoints (INV-DLQ-1/2/3/4, doc 07 §4).

Exposes the three governed actions that advance a dead-letter record through
its lifecycle:

* ``GET  /dlq/{dead_letter_id}``          — inspect a dead-letter record
* ``POST /dlq/{dead_letter_id}/propose``  — attach a diagnosis + proposal_id
                                            (open → investigating, INV-DLQ-2)
* ``POST /dlq/{dead_letter_id}/resolve``  — close the record with explicit
                                            human/policy approval
                                            (investigating → resolved, INV-DLQ-3)

INV-DLQ-1: a failed task lands in ``dead_letters`` in state ``open``; nothing
here auto-resolves it.
INV-DLQ-2: ``propose`` advances the record to ``investigating`` and stores the
diagnosis; it never mutates the live workflow spec.
INV-DLQ-3: ``resolve`` requires ``explicit_approval: true`` in the request
body — the gate is surfaced at the API boundary so the caller cannot omit it by
accident. Any attempt with ``explicit_approval: false`` is rejected with 422.
INV-DLQ-4: promoting a corrected spec is a new workflow version, not an in-place
edit; that is the responsibility of the scheduler-side correction flow (a later
step). This router governs only the DLQ record's state machine.

The router follows the same session/context wiring as the execution router: the
``AuthenticationMiddleware`` writes ``org_context`` and ``org_session`` onto
``request.state`` before any handler runs.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext
from xiosync.services.workflows import (
    DeadLetterNotFoundError,
    WorkflowService,
)

router = APIRouter(prefix="/dlq", tags=["dlq"])


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


class DeadLetterResponse(StrictModel):
    """Read-model for a dead_letters row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    task_id: uuid.UUID
    state: str
    failure_reason: str | None = None
    proposal_id: uuid.UUID | None = None
    diagnosis: dict[str, Any] | None = None


class ProposeRequest(StrictModel):
    """Body for ``POST /dlq/{dead_letter_id}/propose`` (INV-DLQ-2).

    ``diagnosis`` is an advisory machine-readable blob — e.g. the output of
    the self-learning engine — that describes the suspected failure cause and
    an optional suggested corrected workflow spec. It MUST NOT trigger any
    auto-mutation; it is recorded as advisory data only.
    """

    diagnosis: dict[str, Any] = Field(
        description=(
            "Advisory diagnosis from the self-learning engine or a human operator. "
            "Stored on the dead-letter record; does not mutate the live workflow spec "
            "(INV-DLQ-2)."
        )
    )


class ProposeResponse(StrictModel):
    dead_letter_id: uuid.UUID
    proposal_id: uuid.UUID
    state: str


class ResolveRequest(StrictModel):
    """Body for ``POST /dlq/{dead_letter_id}/resolve`` (INV-DLQ-3).

    ``explicit_approval`` MUST be ``true``. The field is required and its
    value is validated here so the caller cannot accidentally omit approval
    (INV-DLQ-3: auto-resolution is never permitted).
    """

    explicit_approval: bool = Field(
        description=(
            "Must be true. Auto-resolution (false) is never permitted (INV-DLQ-3). "
            "Passing false is a validation error."
        )
    )

    @model_validator(mode="after")
    def _require_explicit(self) -> ResolveRequest:
        if not self.explicit_approval:
            raise ValueError(
                "explicit_approval must be true; auto-resolution is forbidden (INV-DLQ-3)"
            )
        return self


class ResolveResponse(StrictModel):
    dead_letter_id: uuid.UUID
    state: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{dead_letter_id}",
    response_model=DeadLetterResponse,
    summary="Fetch a dead-letter record",
)
def get_dead_letter(
    dead_letter_id: uuid.UUID,
    request: Request,
) -> DeadLetterResponse | JSONResponse:
    """Return the current state of one dead-letter record in this organization."""
    context = _context(request)
    service = _service(request)

    record = service.get_dead_letter(context, dead_letter_id)
    if record is None:
        return _problem(request, 404, "dead_letter_not_found", "Dead-letter record not found")

    return DeadLetterResponse(
        id=record.id,
        organization_id=record.organization_id,
        task_id=record.task_id,
        state=record.state,
        failure_reason=record.failure_reason,
        proposal_id=record.proposal_id,
        diagnosis=record.diagnosis,
    )


@router.post(
    "/{dead_letter_id}/propose",
    response_model=ProposeResponse,
    summary="Attach a correction proposal (INV-DLQ-2): open → investigating",
)
def propose_dlq_correction(
    dead_letter_id: uuid.UUID,
    payload: ProposeRequest,
    request: Request,
) -> ProposeResponse | JSONResponse:
    """Advance a dead-letter record from ``open`` to ``investigating``.

    INV-DLQ-2: a correction proposal may only be attached to an ``open``
    record. The diagnosis is stored on the row and a ``proposal_id`` is
    generated. No workflow spec is mutated here.
    """
    context = _context(request)
    service = _service(request)

    try:
        proposal_id = service.propose_dlq_correction(
            context,
            dead_letter_id,
            diagnosis=payload.diagnosis,
        )
    except DeadLetterNotFoundError:
        return _problem(request, 404, "dead_letter_not_found", "Dead-letter record not found")
    except ValueError as exc:
        return _problem(
            request,
            409,
            "proposal_not_accepted",
            "Dead-letter record cannot accept a new proposal in its current state",
            detail=str(exc),
        )

    return ProposeResponse(
        dead_letter_id=dead_letter_id,
        proposal_id=proposal_id,
        state="investigating",
    )


@router.post(
    "/{dead_letter_id}/resolve",
    response_model=ResolveResponse,
    summary="Resolve a dead-letter record (INV-DLQ-3): investigating → resolved",
)
def resolve_dead_letter(
    dead_letter_id: uuid.UUID,
    payload: ResolveRequest,
    request: Request,
) -> ResolveResponse | JSONResponse:
    """Close a dead-letter record, gated by explicit human/policy approval.

    INV-DLQ-3: ``explicit_approval`` must be ``true`` (validated by the
    request model). The record must be in ``investigating`` state — a proposal
    must have been submitted first. Returning ``explicit_approval: false`` is
    caught by Pydantic model validation and surfaces as a 422 before the
    service layer is reached.
    """
    context = _context(request)
    service = _service(request)

    try:
        service.resolve_dead_letter(
            context,
            dead_letter_id,
            explicit_approval=payload.explicit_approval,
        )
    except DeadLetterNotFoundError:
        return _problem(request, 404, "dead_letter_not_found", "Dead-letter record not found")
    except ValueError as exc:
        return _problem(
            request,
            409,
            "resolve_not_permitted",
            "Dead-letter record cannot be resolved in its current state",
            detail=str(exc),
        )

    return ResolveResponse(dead_letter_id=dead_letter_id, state="resolved")
