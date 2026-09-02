import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from xiosync.domain.context import OrgContext, MembershipRole, PlatformRole
from xiosync.persistence.models.projects import Project
from xiosync.services.projects import (
    ProjectNotFoundError,
    ProjectService,
)


@pytest.fixture
def org_context() -> OrgContext:
    return OrgContext(
        auth_identity_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    session.scalar.return_value = 0
    return session


@pytest.fixture
def project_service(mock_session: MagicMock) -> ProjectService:
    svc = ProjectService(mock_session)
    svc._events = MagicMock()
    svc._operations = MagicMock()
    svc._operations.record_operation.return_value = uuid.uuid4()
    return svc


def test_create_project_success(
    project_service: ProjectService,
    org_context: OrgContext,
    mock_session: MagicMock,
) -> None:
    record = project_service.create_project(
        org_context,
        name="Test Project",
        slug="test-project",
        description="A test project",
        config={"key": "value"},
    )
    assert record.name == "Test Project"
    assert record.slug == "test-project"
    assert record.description == "A test project"
    assert record.config == {"key": "value"}
    assert record.state == "active"
    assert record.organization_id == org_context.organization_id
    assert mock_session.add.call_count == 1
    assert mock_session.flush.call_count >= 1


def test_create_project_defaults(
    project_service: ProjectService,
    org_context: OrgContext,
) -> None:
    record = project_service.create_project(
        org_context,
        name="Minimal",
        slug="minimal",
    )
    assert record.description is None
    assert record.config is None


def test_get_project_success(
    project_service: ProjectService,
    org_context: OrgContext,
    mock_session: MagicMock,
) -> None:
    project_id = uuid.uuid4()
    mock_project = Project(
        id=project_id,
        organization_id=org_context.organization_id,
        name="Fetch Me",
        slug="fetch-me",
        state="active",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = mock_project

    fetched = project_service.get_project(org_context, project_id)
    assert fetched.id == project_id
    assert fetched.name == "Fetch Me"


def test_get_project_not_found(
    project_service: ProjectService,
    org_context: OrgContext,
    mock_session: MagicMock,
) -> None:
    mock_session.scalar.return_value = None
    with pytest.raises(ProjectNotFoundError):
        project_service.get_project(org_context, uuid.uuid4())


def test_list_projects(
    project_service: ProjectService,
    org_context: OrgContext,
    mock_session: MagicMock,
) -> None:
    p1 = Project(id=uuid.uuid4(), organization_id=org_context.organization_id, name="P1", slug="p1", state="active", created_at=datetime.now(UTC))
    p2 = Project(id=uuid.uuid4(), organization_id=org_context.organization_id, name="P2", slug="p2", state="active", created_at=datetime.now(UTC))
    
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [p1, p2]
    mock_session.scalars.return_value = mock_scalars

    projects = project_service.list_projects(org_context)
    assert len(projects) == 2
    slugs = {p.slug for p in projects}
    assert "p1" in slugs
    assert "p2" in slugs


def test_update_project_success(
    project_service: ProjectService,
    org_context: OrgContext,
    mock_session: MagicMock,
) -> None:
    project_id = uuid.uuid4()
    mock_project = Project(
        id=project_id,
        organization_id=org_context.organization_id,
        name="Old Name",
        slug="old-slug",
        state="active",
        created_at=datetime.now(UTC),
    )
    
    mock_session.scalar.return_value = mock_project
    
    updated = project_service.update_project(
        org_context,
        project_id,
        name="New Name",
        description="New Desc",
        config={"new": "conf"},
    )
    assert mock_session.execute.call_count == 1
    assert mock_session.flush.call_count >= 1


def test_update_project_not_found(
    project_service: ProjectService,
    org_context: OrgContext,
    mock_session: MagicMock,
) -> None:
    mock_session.scalar.return_value = None
    with pytest.raises(ProjectNotFoundError):
        project_service.update_project(org_context, uuid.uuid4(), name="X")


def test_archive_project_success(
    project_service: ProjectService,
    org_context: OrgContext,
    mock_session: MagicMock,
) -> None:
    project_id = uuid.uuid4()
    mock_project = Project(
        id=project_id,
        organization_id=org_context.organization_id,
        name="To Archive",
        slug="to-archive",
        state="active",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = mock_project
    
    archived = project_service.archive_project(org_context, project_id)
    assert mock_session.execute.call_count == 1


def test_archive_project_already_archived(
    project_service: ProjectService,
    org_context: OrgContext,
    mock_session: MagicMock,
) -> None:
    project_id = uuid.uuid4()
    mock_project = Project(
        id=project_id,
        organization_id=org_context.organization_id,
        name="To Archive",
        slug="to-archive",
        state="archived",
        created_at=datetime.now(UTC),
    )
    mock_session.scalar.return_value = mock_project
    
    archived = project_service.archive_project(org_context, project_id)
    assert mock_session.execute.call_count == 0


def test_archive_project_not_found(
    project_service: ProjectService,
    org_context: OrgContext,
    mock_session: MagicMock,
) -> None:
    mock_session.scalar.return_value = None
    with pytest.raises(ProjectNotFoundError):
        project_service.archive_project(org_context, uuid.uuid4())
