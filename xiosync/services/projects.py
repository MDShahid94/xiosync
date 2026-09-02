"""Project isolation service."""
from __future__ import annotations
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from xiosync.domain.context import OrgContext
from xiosync.domain.events import build_state_change_payload
from xiosync.persistence.models.projects import Project
from xiosync.persistence.operations import OperationRepository
from xiosync.platform.ids import new_id
from xiosync.services.events import EventService
from xiosync.services.operations import OperationService

__all__ = ["ProjectNotFoundError", "ProjectRecord", "ProjectService"]

@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    config: dict[str, Any] | None
    state: str
    created_at: datetime
    updated_at: datetime | None

class ProjectNotFoundError(ValueError):
    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__(f"Project {project_id} not found")

def _record(row: Project) -> ProjectRecord:
    return ProjectRecord(
        id=row.id,
        organization_id=row.organization_id,
        name=row.name,
        slug=row.slug,
        description=row.description,
        config=dict(row.config) if row.config else None,
        state=row.state,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )

class ProjectService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._operations = OperationService(OperationRepository(session))
        self._events = EventService(session)

    def create_project(self, context: OrgContext, *, name: str, slug: str, description: str | None = None, config: Mapping[str, Any] | None = None) -> ProjectRecord:
        now = datetime.now(UTC)
        project = Project(
            id=new_id(), organization_id=context.organization_id, name=name, slug=slug,
            description=description, config=dict(config) if config else None,
            state="active", created_at=now
        )
        self._session.add(project)
        op_id = self._operations.record_operation(context, actor_id=context.actor_id, operation="project.create", trigger="user_command", initiated_by=context.actor_id, scope="organization", outcome="success", rationale=f"Created project: {name}")
        self._events.append(context, event_type="project.created", actor_id=context.actor_id, severity="info", operation_id=op_id, entity_type="project", entity_id=project.id, payload={"name": name, "slug": slug})
        self._session.flush()
        return _record(project)

    def update_project(self, context: OrgContext, project_id: uuid.UUID, *, name: str | None = None, description: str | None = None, config: Mapping[str, Any] | None = None) -> ProjectRecord:
        row = self._session.scalar(select(Project).where(Project.organization_id == context.organization_id, Project.id == project_id))
        if row is None: raise ProjectNotFoundError(project_id)
        if name is not None: row.name = name
        if description is not None: row.description = description
        if config is not None: row.config = dict(config)
        row.updated_at = datetime.now(UTC)
        op_id = self._operations.record_operation(context, actor_id=context.actor_id, operation="project.update", trigger="user_command", initiated_by=context.actor_id, scope="organization", outcome="success", rationale=f"Updated project: {row.name}")
        self._events.append(context, event_type="project.updated", actor_id=context.actor_id, severity="info", operation_id=op_id, entity_type="project", entity_id=project_id, payload={})
        self._session.flush()
        return _record(row)

    def get_project(self, context: OrgContext, project_id: uuid.UUID) -> ProjectRecord:
        row = self._session.scalar(select(Project).where(Project.organization_id == context.organization_id, Project.id == project_id))
        if row is None: raise ProjectNotFoundError(project_id)
        return _record(row)

    def list_projects(self, context: OrgContext) -> list[ProjectRecord]:
        rows = self._session.scalars(select(Project).where(Project.organization_id == context.organization_id).order_by(Project.created_at.desc())).all()
        return [_record(row) for row in rows]

    def archive_project(self, context: OrgContext, project_id: uuid.UUID) -> ProjectRecord:
        project = self._session.scalar(select(Project).where(Project.id == project_id, Project.organization_id == context.organization_id))
        if project is None: raise ProjectNotFoundError(project_id)
        if project.state == "archived": return _record(project)
        old_state = project.state
        project.state = "archived"
        project.updated_at = datetime.now(UTC)
        op_id = self._operations.record_operation(context, actor_id=context.actor_id, operation="project.archive", trigger="user_command", initiated_by=context.actor_id, scope="organization", outcome="success", rationale=f"Archived project: {project.name}")
        self._events.append(context, event_type="project.archived", actor_id=context.actor_id, severity="warning", operation_id=op_id, entity_type="project", entity_id=project_id, payload=build_state_change_payload(entity_type="project", entity_id=project_id, from_state=old_state, to_state="archived", operation_id=op_id, trigger="user_command", severity="warning"))
        self._session.flush()
        return _record(project)
