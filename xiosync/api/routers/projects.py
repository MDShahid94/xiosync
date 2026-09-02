"""Project management endpoints."""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext
from xiosync.services.projects import (
    ProjectNotFoundError,
    ProjectRecord,
    ProjectService,
)

router = APIRouter(prefix="/projects", tags=["projects"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreateRequest(StrictModel):
    name: str = Field(..., description="Name of the project")
    slug: str = Field(..., description="Slug of the project")
    description: str | None = Field(default=None, description="Optional description")
    config: dict[str, Any] | None = Field(default=None, description="Optional configuration JSON")


class ProjectUpdateRequest(StrictModel):
    name: str | None = Field(default=None, description="Updated name")
    slug: str | None = Field(default=None, description="Updated slug")
    description: str | None = Field(default=None, description="Updated description")
    config: dict[str, Any] | None = Field(default=None, description="Updated configuration JSON")


class ProjectResponse(StrictModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    config: dict[str, Any] | None
    state: str
    created_at: str
    updated_at: str | None


def _context(request: Request) -> OrgContext:
    return cast(OrgContext, request.state.org_context)


def _session(request: Request) -> OrmSession:
    return cast(OrmSession, request.state.org_session)


def _service(request: Request) -> ProjectService:
    return ProjectService(_session(request))


def _to_response(record: ProjectRecord) -> ProjectResponse:
    return ProjectResponse(
        id=record.id,
        organization_id=record.organization_id,
        name=record.name,
        slug=record.slug,
        description=record.description,
        config=record.config,
        state=record.state,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat() if record.updated_at else None,
    )


def _problem(
    request: Request,
    status: int,
    code: str,
    title: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"https://xiosync.dev/problems/{code}",
            "title": title,
            "status": status,
            "code": code,
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@router.post("", response_model=ProjectResponse, status_code=201)
def create_project(request: Request, payload: ProjectCreateRequest) -> ProjectResponse:
    """Create a new project."""
    service = _service(request)
    record = service.create_project(
        _context(request),
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        config=payload.config,
    )
    return _to_response(record)


@router.get("", response_model=list[ProjectResponse])
def list_projects(request: Request) -> list[ProjectResponse]:
    """List all projects in the organization."""
    service = _service(request)
    records = service.list_projects(_context(request))
    return [_to_response(r) for r in records]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(request: Request, project_id: uuid.UUID) -> ProjectResponse | JSONResponse:
    """Get a specific project."""
    service = _service(request)
    try:
        record = service.get_project(_context(request), project_id)
        return _to_response(record)
    except ProjectNotFoundError:
        return _problem(request, 404, "project_not_found", "Project not found")


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    request: Request, project_id: uuid.UUID, payload: ProjectUpdateRequest
) -> ProjectResponse | JSONResponse:
    """Update a project."""
    service = _service(request)
    try:
        record = service.update_project(
            _context(request),
            project_id,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            config=payload.config,
        )
        return _to_response(record)
    except ProjectNotFoundError:
        return _problem(request, 404, "project_not_found", "Project not found")


@router.post("/{project_id}/archive", response_model=ProjectResponse)
def archive_project(request: Request, project_id: uuid.UUID) -> ProjectResponse | JSONResponse:
    """Archive a project."""
    service = _service(request)
    try:
        record = service.archive_project(_context(request), project_id)
        return _to_response(record)
    except ProjectNotFoundError:
        return _problem(request, 404, "project_not_found", "Project not found")
