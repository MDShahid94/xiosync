"""Workflow CRUD API endpoints (Phase 2 — Gap G-4)."""
from __future__ import annotations
import uuid
from typing import Any, cast
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["workflows"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateWorkflowRequest(_S):
    name: str
    spec: dict[str, Any] | None = Field(default=None, description="Workflow DAG specification")

class StartRunRequest(_S):
    pass

class EnqueueTaskRequest(_S):
    run_id: uuid.UUID
    node_id: str
    capability_id: uuid.UUID
    input: dict[str, Any] | None = None
    priority: int = 5

@router.post("/workflows", status_code=201, summary="Create a workflow")
def create_workflow(payload: CreateWorkflowRequest, request: Request) -> dict[str, Any]:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.workflows import WorkflowService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WorkflowService(session)
    wf_id = svc.create_workflow(ctx, name=payload.name, created_by=ctx.actor_id, spec=payload.spec)
    return {"id": str(wf_id), "state": "draft"}

@router.get("/workflows/{workflow_id}", summary="Get a workflow by ID", response_model=None)
def get_workflow(workflow_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.workflows import WorkflowService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WorkflowService(session)
    rec = svc.get_workflow(ctx, workflow_id)
    if rec is None:
        return JSONResponse(status_code=404, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/not_found", "title": "Workflow not found", "status": 404,
        })
    return {"id": str(rec.id), "name": rec.name, "state": rec.state, "spec": rec.spec,
            "created_by": str(rec.created_by)}

@router.post("/workflows/{workflow_id}/publish", summary="Publish a workflow", response_model=None)
def publish_workflow(workflow_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.workflows import WorkflowService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WorkflowService(session)
    try:
        svc.publish_workflow(ctx, workflow_id)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/publish_error", "title": "Publish failed",
            "status": 422, "detail": str(exc),
        })
    return {"workflow_id": str(workflow_id), "state": "published"}

@router.post("/workflows/{workflow_id}/runs", status_code=201, summary="Start a workflow run", response_model=None)
def start_run(workflow_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.workflows import WorkflowService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WorkflowService(session)
    try:
        run_id = svc.start_run(ctx, workflow_id, initiated_by=ctx.actor_id)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/run_error", "title": "Start run failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(run_id), "workflow_id": str(workflow_id), "state": "queued"}

@router.post("/tasks", status_code=201, summary="Enqueue a task", response_model=None)
def enqueue_task(payload: EnqueueTaskRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.workflows import WorkflowService
    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = WorkflowService(session)
    try:
        task_id = svc.enqueue_task(ctx, payload.run_id, node_id=payload.node_id,
                                   capability_id=payload.capability_id, input=payload.input,
                                   priority=payload.priority)
    except Exception as exc:
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/task_error", "title": "Enqueue failed",
            "status": 422, "detail": str(exc),
        })
    return {"id": str(task_id), "state": "queued"}
