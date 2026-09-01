"""Real-PostgreSQL tests for the Workflow service (docs 03 §§2.11, 4.4; 07).

Runs the full ``WorkflowService`` stack — pure DAG predicate + tenant-scoped
ORM writes — against a scratch database migrated by Alembic alone, entered
through the canonical ``org_scoped_session`` RLS gate. Seeding uses the admin
(superuser) connection (which bypasses RLS by design); every service call and
assertion runs as the plain application role inside the org scope.

These prove, on the real schema:

* INV-WF-1 — a workflow whose ``spec`` is a valid DAG can be published, and a
  cyclic (or otherwise malformed) spec is rejected on publish with the row left
  untouched in ``draft``; and
* INV-DLQ-1 — a failed task lands in ``dead_letters`` in state ``open`` while
  the task itself moves to the terminal ``dead_letter`` state, with nothing
  auto-resolving the record.

Step 2 extends coverage to the full lease protocol and DLQ governance:

* INV-EXEC-1 — ``lease_task`` atomically transitions ``queued`` → ``leased``
  and only ``queued`` tasks are leaseable.
* INV-EXEC-2 — ``complete_task`` is idempotent: a second completion returns
  ``CompletionOutcome(duplicate=True)`` instead of raising.
* Heartbeat — ``heartbeat_task`` extends an active lease; a wrong/expired
  lease_id raises ``InactiveLeaseError``.
* Expiry reconciler — ``expire_leases`` reclaims ``leased`` tasks whose wall-
  clock has passed, returning them to ``queued``.
* INV-DLQ-2 — ``propose_dlq_correction`` advances ``open`` → ``investigating``
  and attaches a diagnosis; re-proposing while ``investigating`` raises.
* INV-DLQ-3 — ``resolve_dead_letter`` requires ``explicit_approval=True`` and
  state ``investigating``; anything else raises.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.domain.workflows import WorkflowCycleError, WorkflowSpecError
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.workflows import (
    InactiveLeaseError,
    UnleaseableError,
    WorkflowService,
)

pytestmark = pytest.mark.integration


def _seed_org_actor_capability(
    admin_url: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed one active org, one active actor, and one capability.

    Returns ``(org_id, actor_id, capability_id)``. Tasks reference a capability
    and (when leased) an actor, so both must exist in the org before a run's
    tasks can be enqueued.
    """
    organization_id = new_id()
    actor_id = new_id()
    capability_id = new_id()
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'Workflows Test', 'active')"
                ),
                {"id": organization_id, "slug": f"wf-{organization_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO actors (id, organization_id, actor_type, state, "
                    "lifecycle_phase, trust_tier, health_status) VALUES "
                    "(:id, :org, 'ai', 'active', 'operational', 'trusted', 'healthy')"
                ),
                {"id": actor_id, "org": organization_id},
            )
            connection.execute(
                text(
                    "INSERT INTO capabilities (id, organization_id, name) "
                    "VALUES (:id, :org, 'fetch')"
                ),
                {"id": capability_id, "org": organization_id},
            )
    finally:
        engine.dispose()
    return organization_id, actor_id, capability_id


def _context(organization_id: uuid.UUID, actor_id: uuid.UUID) -> OrgContext:
    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=actor_id,
        organization_id=organization_id,
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_MEMBER,
    )


# A minimal linear DAG: fetch -> transform -> load.
_LINEAR_DAG = {
    "nodes": [{"id": "fetch"}, {"id": "transform"}, {"id": "load"}],
    "edges": [
        {"from": "fetch", "to": "transform"},
        {"from": "transform", "to": "load"},
    ],
}

# The same three nodes closed into a cycle: load -> fetch.
_CYCLIC_DAG = {
    "nodes": [{"id": "fetch"}, {"id": "transform"}, {"id": "load"}],
    "edges": [
        {"from": "fetch", "to": "transform"},
        {"from": "transform", "to": "load"},
        {"from": "load", "to": "fetch"},
    ],
}


def _state(engine: Engine, context: OrgContext, table: str, row_id: uuid.UUID) -> str:
    with org_scoped_session(engine, context) as session:
        state = session.execute(
            text(f"SELECT state FROM {table} WHERE id = :id"),  # noqa: S608 - table is a literal
            {"id": row_id},
        ).scalar_one()
    return str(state)


def test_publish_valid_dag_succeeds(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-WF-1: a workflow with an acyclic spec publishes cleanly."""
    organization_id, actor_id, _ = _seed_org_actor_capability(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            workflow_id = service.create_workflow(
                context, name="etl", created_by=actor_id, spec=_LINEAR_DAG
            )

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            service.publish_workflow(context, workflow_id)

        assert _state(engine, context, "workflows", workflow_id) == "published"
    finally:
        engine.dispose()


def test_publish_cyclic_spec_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-WF-1: publishing a cyclic spec is rejected; the row stays draft."""
    organization_id, actor_id, _ = _seed_org_actor_capability(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            workflow_id = service.create_workflow(
                context, name="cyclic", created_by=actor_id, spec=_CYCLIC_DAG
            )

        with pytest.raises(WorkflowCycleError):
            with org_scoped_session(engine, context) as session:
                service = WorkflowService(session)
                service.publish_workflow(context, workflow_id)

        # The rejected publish left the workflow untouched in draft.
        assert _state(engine, context, "workflows", workflow_id) == "draft"
    finally:
        engine.dispose()


def test_publish_malformed_spec_is_rejected(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-WF-1: an edge referencing an unknown node is a spec error, not published."""
    organization_id, actor_id, _ = _seed_org_actor_capability(migrated_database_url)
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        dangling = {
            "nodes": [{"id": "fetch"}],
            "edges": [{"from": "fetch", "to": "ghost"}],
        }
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            workflow_id = service.create_workflow(
                context, name="dangling", created_by=actor_id, spec=dangling
            )

        with pytest.raises(WorkflowSpecError):
            with org_scoped_session(engine, context) as session:
                service = WorkflowService(session)
                service.publish_workflow(context, workflow_id)

        assert _state(engine, context, "workflows", workflow_id) == "draft"
    finally:
        engine.dispose()


def test_failed_task_lands_in_dlq_open(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-DLQ-1: a dead-lettered task lands 'open' and the task goes terminal."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            workflow_id = service.create_workflow(
                context, name="etl", created_by=actor_id, spec=_LINEAR_DAG
            )
            run_id = service.start_run(context, workflow_id, initiated_by=actor_id)
            task_id = service.enqueue_task(
                context, run_id, node_id="fetch", capability_id=capability_id
            )

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            dead_letter_id = service.dead_letter_task(
                context, task_id, failure_reason="capability timed out"
            )

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            task = service.get_task(context, task_id)
            dead_letter = service.get_dead_letter(context, dead_letter_id)

        assert task is not None
        assert task.state == "dead_letter"
        assert dead_letter is not None
        assert dead_letter.task_id == task_id
        assert dead_letter.state == "open"
        assert dead_letter.failure_reason == "capability timed out"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Step 2: lease protocol
# ---------------------------------------------------------------------------


def _create_and_enqueue(
    engine: Engine,
    context: OrgContext,
    actor_id: uuid.UUID,
    capability_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create+publish a workflow, start a run, enqueue one task.

    Returns ``(workflow_id, run_id, task_id)``.
    """
    with org_scoped_session(engine, context) as session:
        service = WorkflowService(session)
        workflow_id = service.create_workflow(
            context, name="etl", created_by=actor_id, spec=_LINEAR_DAG
        )
        service.publish_workflow(context, workflow_id)
        run_id = service.start_run(context, workflow_id, initiated_by=actor_id)
        task_id = service.enqueue_task(
            context, run_id, node_id="fetch", capability_id=capability_id
        )
    return workflow_id, run_id, task_id


def test_lease_task_queued_to_leased(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EXEC-1: lease_task transitions queued→leased and populates lease fields."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, _, task_id = _create_and_enqueue(engine, context, actor_id, capability_id)
        now = datetime.now(UTC)
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            task = service.lease_task(
                context,
                task_id,
                leased_by=actor_id,
                duration=timedelta(minutes=5),
                now=now,
            )

        assert task.state == "leased"
        assert task.attempts == 1
        assert task.lease_id is not None
        assert task.leased_by == actor_id
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > now
    finally:
        engine.dispose()


def test_lease_task_not_leaseable_raises(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EXEC-1: leasing an already-leased task raises UnleaseableError."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, _, task_id = _create_and_enqueue(engine, context, actor_id, capability_id)
        now = datetime.now(UTC)

        # First lease succeeds.
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            service.lease_task(
                context,
                task_id,
                leased_by=actor_id,
                duration=timedelta(minutes=5),
                now=now,
            )

        # Second lease on the same (now 'leased') task must raise.
        with pytest.raises(UnleaseableError) as exc_info:
            with org_scoped_session(engine, context) as session:
                service = WorkflowService(session)
                service.lease_task(
                    context,
                    task_id,
                    leased_by=actor_id,
                    duration=timedelta(minutes=5),
                    now=now,
                )

        assert exc_info.value.state == "leased"
    finally:
        engine.dispose()


def test_heartbeat_extends_lease(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """Heartbeat with the correct lease_id pushes lease_expires_at forward."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, _, task_id = _create_and_enqueue(engine, context, actor_id, capability_id)
        now = datetime.now(UTC)

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            leased = service.lease_task(
                context,
                task_id,
                leased_by=actor_id,
                duration=timedelta(minutes=5),
                now=now,
            )
        original_expires_at = leased.lease_expires_at
        assert original_expires_at is not None

        # Heartbeat with a later 'now' and a longer duration.
        heartbeat_now = now + timedelta(minutes=1)
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            heartbeated = service.heartbeat_task(
                context,
                task_id,
                lease_id=leased.lease_id,  # type: ignore[arg-type]
                duration=timedelta(minutes=10),
                now=heartbeat_now,
            )

        assert heartbeated.lease_expires_at is not None
        assert heartbeated.lease_expires_at > original_expires_at
    finally:
        engine.dispose()


def test_heartbeat_wrong_lease_id_raises(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """Heartbeat with a mismatched lease_id raises InactiveLeaseError."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, _, task_id = _create_and_enqueue(engine, context, actor_id, capability_id)
        now = datetime.now(UTC)

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            service.lease_task(
                context,
                task_id,
                leased_by=actor_id,
                duration=timedelta(minutes=5),
                now=now,
            )

        wrong_lease_id = uuid.uuid4()
        with pytest.raises(InactiveLeaseError):
            with org_scoped_session(engine, context) as session:
                service = WorkflowService(session)
                service.heartbeat_task(
                    context,
                    task_id,
                    lease_id=wrong_lease_id,
                    duration=timedelta(minutes=5),
                    now=now,
                )
    finally:
        engine.dispose()


def test_complete_task_normal_path(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EXEC-2: completing a leased task returns duplicate=False and state='completed'."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, _, task_id = _create_and_enqueue(engine, context, actor_id, capability_id)
        now = datetime.now(UTC)

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            leased = service.lease_task(
                context,
                task_id,
                leased_by=actor_id,
                duration=timedelta(minutes=5),
                now=now,
            )

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            outcome = service.complete_task(
                context,
                task_id,
                lease_id=leased.lease_id,  # type: ignore[arg-type]
                now=now,
            )

        assert outcome.duplicate is False
        assert outcome.state == "completed"

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            task = service.get_task(context, task_id)

        assert task is not None
        assert task.state == "completed"
        assert task.lease_expires_at is None
    finally:
        engine.dispose()


def test_complete_task_idempotent(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-EXEC-2: completing an already-completed task returns duplicate=True."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, _, task_id = _create_and_enqueue(engine, context, actor_id, capability_id)
        now = datetime.now(UTC)

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            leased = service.lease_task(
                context,
                task_id,
                leased_by=actor_id,
                duration=timedelta(minutes=5),
                now=now,
            )
        lease_id = leased.lease_id

        # First completion — normal path.
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            first = service.complete_task(
                context, task_id, lease_id=lease_id, now=now  # type: ignore[arg-type]
            )
        assert first.duplicate is False

        # Second completion — idempotent path.
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            second = service.complete_task(
                context, task_id, lease_id=lease_id, now=now  # type: ignore[arg-type]
            )
        assert second.duplicate is True
    finally:
        engine.dispose()


def test_complete_task_wrong_lease_id_raises(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """complete_task with a mismatched lease_id raises InactiveLeaseError."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, _, task_id = _create_and_enqueue(engine, context, actor_id, capability_id)
        now = datetime.now(UTC)

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            service.lease_task(
                context,
                task_id,
                leased_by=actor_id,
                duration=timedelta(minutes=5),
                now=now,
            )

        wrong_lease_id = uuid.uuid4()
        with pytest.raises(InactiveLeaseError):
            with org_scoped_session(engine, context) as session:
                service = WorkflowService(session)
                service.complete_task(
                    context, task_id, lease_id=wrong_lease_id, now=now
                )
    finally:
        engine.dispose()


def test_expire_leases_reclaims_task(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """expire_leases reclaims an expired task back to 'queued' and clears lease fields."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, _, task_id = _create_and_enqueue(engine, context, actor_id, capability_id)
        now = datetime.now(UTC)

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            leased = service.lease_task(
                context,
                task_id,
                leased_by=actor_id,
                duration=timedelta(seconds=1),
                now=now,
            )
        assert leased.lease_expires_at is not None

        # Advance clock past expiry.
        after_expiry = leased.lease_expires_at + timedelta(seconds=1)

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            reclaimed_ids = service.expire_leases(context, now=after_expiry)

        assert task_id in reclaimed_ids

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            task = service.get_task(context, task_id)

        assert task is not None
        assert task.state == "queued"
        assert task.lease_id is None
        assert task.leased_by is None
        assert task.lease_expires_at is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Step 2: DLQ governance
# ---------------------------------------------------------------------------


def _enqueue_and_dead_letter(
    engine: Engine,
    context: OrgContext,
    actor_id: uuid.UUID,
    capability_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Enqueue a task and immediately dead-letter it.

    Returns ``(task_id, dead_letter_id)``.
    """
    _, _, task_id = _create_and_enqueue(engine, context, actor_id, capability_id)
    with org_scoped_session(engine, context) as session:
        service = WorkflowService(session)
        dead_letter_id = service.dead_letter_task(
            context, task_id, failure_reason="test failure"
        )
    return task_id, dead_letter_id


def test_propose_dlq_correction_advances_state(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-DLQ-2: propose_dlq_correction advances open→investigating and stores diagnosis."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, dead_letter_id = _enqueue_and_dead_letter(
            engine, context, actor_id, capability_id
        )

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            proposal_id = service.propose_dlq_correction(
                context,
                dead_letter_id,
                diagnosis={"root_cause": "timeout"},
            )

        assert isinstance(proposal_id, uuid.UUID)

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            dl = service.get_dead_letter(context, dead_letter_id)

        assert dl is not None
        assert dl.state == "investigating"
        assert dl.proposal_id == proposal_id
    finally:
        engine.dispose()


def test_propose_dlq_correction_non_open_raises(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-DLQ-2: proposing a correction when state='investigating' raises ValueError."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, dead_letter_id = _enqueue_and_dead_letter(
            engine, context, actor_id, capability_id
        )

        # First proposal advances to 'investigating'.
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            service.propose_dlq_correction(
                context,
                dead_letter_id,
                diagnosis={"root_cause": "timeout"},
            )

        # Second proposal on an already-investigating record must raise.
        with pytest.raises(ValueError):
            with org_scoped_session(engine, context) as session:
                service = WorkflowService(session)
                service.propose_dlq_correction(
                    context,
                    dead_letter_id,
                    diagnosis={"root_cause": "retry"},
                )
    finally:
        engine.dispose()


def test_resolve_dead_letter_requires_explicit_approval(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-DLQ-3: resolve_dead_letter with explicit_approval=False raises ValueError."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, dead_letter_id = _enqueue_and_dead_letter(
            engine, context, actor_id, capability_id
        )

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            service.propose_dlq_correction(
                context,
                dead_letter_id,
                diagnosis={"root_cause": "timeout"},
            )

        with pytest.raises(ValueError):
            with org_scoped_session(engine, context) as session:
                service = WorkflowService(session)
                service.resolve_dead_letter(
                    context, dead_letter_id, explicit_approval=False
                )
    finally:
        engine.dispose()


def test_resolve_dead_letter_explicit_approval(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """INV-DLQ-3: resolve_dead_letter with explicit_approval=True resolves the record."""
    organization_id, actor_id, capability_id = _seed_org_actor_capability(
        migrated_database_url
    )
    context = _context(organization_id, actor_id)
    engine = create_engine(app_role_database_url)
    try:
        _, dead_letter_id = _enqueue_and_dead_letter(
            engine, context, actor_id, capability_id
        )

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            service.propose_dlq_correction(
                context,
                dead_letter_id,
                diagnosis={"root_cause": "timeout"},
            )

        # Must not raise.
        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            service.resolve_dead_letter(
                context, dead_letter_id, explicit_approval=True
            )

        with org_scoped_session(engine, context) as session:
            service = WorkflowService(session)
            dl = service.get_dead_letter(context, dead_letter_id)

        assert dl is not None
        assert dl.state == "resolved"
    finally:
        engine.dispose()
