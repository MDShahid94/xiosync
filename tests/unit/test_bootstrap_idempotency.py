"""Unit tests for bootstrap idempotency (improvement #8).

Tests that ``BootstrapService.genesis()`` is idempotent and correctly handles:
1. Creating all expected foundation entities on first run.
2. Detecting existing organization on subsequent runs without creating duplicates.
3. Using consistent, deterministic UUIDs for core system entities.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.identity import (
    Actor,
    AuthIdentity,
    Membership,
    Organization,
)
from xiosync.persistence.models.ontology import TypeRegistry
from xiosync.persistence.models.operations import Operation
from xiosync.persistence.models.registry import CapabilityGroup
from xiosync.services.bootstrap import (
    _CORE_ACTOR_TYPES,
    _CORE_EVENT_TYPES,
    _CORE_LIFECYCLE_STATES,
    _CORE_OPERATION_TYPES,
    _DEFAULT_CAPABILITY_GROUPS,
    AI_AGENT_ACTOR_ID,
    BOOTSTRAP_HUMAN_ACTOR_ID,
    GENESIS_ORG_ID,
    SYSTEM_ACTOR_ID,
    BootstrapService,
    GenesisResult,
)

_NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def _mock_session(*, existing_org: Organization | None = None) -> MagicMock:
    """Create a mock SQLAlchemy Session.

    Configures ``execute(...).scalar_one_or_none()`` to simulate either
    an empty database (first run) or an existing genesis organization (second run).
    """
    session = MagicMock(spec=Session)
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = existing_org
    session.execute.return_value = execute_result
    session.scalar.return_value = existing_org
    return session


def _collect_added_entities(session: MagicMock) -> list[Any]:
    """Collect all model entities passed to ``session.add`` and ``session.add_all``."""
    entities: list[Any] = []
    for call in session.add.call_args_list:
        entities.append(call.args[0])
    for call in session.add_all.call_args_list:
        entities.extend(call.args[0])
    return entities


# =============================================================================
# 1. Deterministic UUID verification
# =============================================================================


def test_deterministic_uuids_values_and_uniqueness() -> None:
    """Verify genesis sentinel UUIDs are valid, distinct, and match expected constants."""
    expected_org_id = uuid.UUID("00000000-0000-7000-8000-000000000000")
    expected_system_id = uuid.UUID("00000000-0000-7000-8000-000000000001")
    expected_human_id = uuid.UUID("00000000-0000-7000-8000-000000000002")
    expected_ai_id = uuid.UUID("00000000-0000-7000-8000-000000000003")

    assert GENESIS_ORG_ID == expected_org_id
    assert SYSTEM_ACTOR_ID == expected_system_id
    assert BOOTSTRAP_HUMAN_ACTOR_ID == expected_human_id
    assert AI_AGENT_ACTOR_ID == expected_ai_id

    # Ensure all sentinel IDs are distinct
    sentinel_ids = {
        GENESIS_ORG_ID,
        SYSTEM_ACTOR_ID,
        BOOTSTRAP_HUMAN_ACTOR_ID,
        AI_AGENT_ACTOR_ID,
    }
    assert len(sentinel_ids) == 4


# =============================================================================
# 2. First-run entity creation tests
# =============================================================================


def test_genesis_first_run_creates_all_expected_entities_without_password() -> None:
    """Verify genesis() creates organization, actors, types, capability groups, operation, and event."""
    session = _mock_session(existing_org=None)
    service = BootstrapService(session)

    result = service.genesis(now=_NOW)

    # Verify returned GenesisResult
    assert isinstance(result, GenesisResult)
    assert result.already_existed is False
    assert result.organization_id == GENESIS_ORG_ID
    assert result.system_actor_id == SYSTEM_ACTOR_ID
    assert result.human_actor_id == BOOTSTRAP_HUMAN_ACTOR_ID
    assert result.ai_agent_actor_id == AI_AGENT_ACTOR_ID
    assert result.auth_identity_id is None

    # Collect all entities added to the session
    added_entities = _collect_added_entities(session)

    # 1. Organization
    orgs = [e for e in added_entities if isinstance(e, Organization)]
    assert len(orgs) == 1
    org = orgs[0]
    assert org.id == GENESIS_ORG_ID
    assert org.slug == "xiosync-system"
    assert org.name == "XIOSYNC System"
    assert org.state == "active"
    assert org.created_at == _NOW

    # 2. Actors (System, Human Developer, AI Agent)
    actors = [e for e in added_entities if isinstance(e, Actor)]
    assert len(actors) == 3
    actor_ids = {a.id for a in actors}
    assert actor_ids == {SYSTEM_ACTOR_ID, BOOTSTRAP_HUMAN_ACTOR_ID, AI_AGENT_ACTOR_ID}

    system_actor = next(a for a in actors if a.id == SYSTEM_ACTOR_ID)
    assert system_actor.actor_type == "system"
    assert system_actor.actor_subtype == "platform"
    assert system_actor.organization_id == GENESIS_ORG_ID
    assert system_actor.state == "active"

    human_actor = next(a for a in actors if a.id == BOOTSTRAP_HUMAN_ACTOR_ID)
    assert human_actor.actor_type == "human"
    assert human_actor.actor_subtype == "developer"
    assert human_actor.organization_id == GENESIS_ORG_ID
    assert human_actor.created_by == SYSTEM_ACTOR_ID

    ai_actor = next(a for a in actors if a.id == AI_AGENT_ACTOR_ID)
    assert ai_actor.actor_type == "ai_agent"
    assert ai_actor.actor_subtype == "developer"
    assert ai_actor.organization_id == GENESIS_ORG_ID
    assert ai_actor.created_by == SYSTEM_ACTOR_ID

    # 3. TypeRegistry entries
    type_entries = [e for e in added_entities if isinstance(e, TypeRegistry)]
    expected_type_count = (
        len(_CORE_ACTOR_TYPES)
        + len(_CORE_EVENT_TYPES)
        + len(_CORE_LIFECYCLE_STATES)
        + len(_CORE_OPERATION_TYPES)
    )
    assert len(type_entries) == expected_type_count
    categories = {t.category for t in type_entries}
    assert categories == {"actor_type", "event_type", "lifecycle_state", "operation_type"}
    for entry in type_entries:
        assert entry.organization_id is None  # Global namespace
        assert entry.namespace == "core"
        assert entry.state == "active"

    # 4. Capability Groups
    cap_groups = [e for e in added_entities if isinstance(e, CapabilityGroup)]
    assert len(cap_groups) == len(_DEFAULT_CAPABILITY_GROUPS)
    cap_group_names = {g.name for g in cap_groups}
    assert "platform.admin" in cap_group_names
    assert "readonly" in cap_group_names
    assert "actor.manage" in cap_group_names
    assert "workflow.manage" in cap_group_names

    # 5. AuthIdentity / Membership should not be created when no password provided
    auth_identities = [e for e in added_entities if isinstance(e, AuthIdentity)]
    memberships = [e for e in added_entities if isinstance(e, Membership)]
    assert len(auth_identities) == 0
    assert len(memberships) == 0

    # 6. Operation & Event
    operations = [e for e in added_entities if isinstance(e, Operation)]
    assert len(operations) == 1
    op = operations[0]
    assert op.organization_id == GENESIS_ORG_ID
    assert op.actor_id == SYSTEM_ACTOR_ID
    assert op.operation == "genesis.bootstrap"
    assert op.outcome == "success"

    events = [e for e in added_entities if isinstance(e, Event)]
    assert len(events) == 1
    event = events[0]
    assert event.organization_id == GENESIS_ORG_ID
    assert event.event_type == "genesis.bootstrap"
    assert event.actor_id == SYSTEM_ACTOR_ID
    assert event.operation_id == op.id

    # Verify flushes occurred
    assert session.flush.call_count >= 1


def test_genesis_first_run_creates_admin_identity_with_password() -> None:
    """Verify genesis() creates admin AuthIdentity and Membership when password is provided."""
    session = _mock_session(existing_org=None)
    service = BootstrapService(session)

    result = service.genesis(
        admin_email="superadmin@xiosync.dev",
        admin_password="super-secret-password",
        now=_NOW,
    )

    assert result.already_existed is False
    assert result.auth_identity_id is not None

    added_entities = _collect_added_entities(session)

    # Verify AuthIdentity created
    identities = [e for e in added_entities if isinstance(e, AuthIdentity)]
    assert len(identities) == 1
    identity = identities[0]
    assert identity.id == result.auth_identity_id
    assert identity.organization_id == GENESIS_ORG_ID
    assert identity.human_actor_id == BOOTSTRAP_HUMAN_ACTOR_ID
    assert identity.email == "superadmin@xiosync.dev"
    assert identity.password_hash != "super-secret-password"  # Must be hashed
    assert identity.state == "active"

    # Verify Membership created
    memberships = [e for e in added_entities if isinstance(e, Membership)]
    assert len(memberships) == 1
    membership = memberships[0]
    assert membership.organization_id == GENESIS_ORG_ID
    assert membership.auth_identity_id == identity.id
    assert membership.membership_role == "org_owner"


# =============================================================================
# 3. Idempotency tests (second run / existing org)
# =============================================================================


def test_genesis_detects_existing_org_and_is_idempotent() -> None:
    """Verify genesis() returns already_existed=True and makes no DB modifications when org exists."""
    existing_org = Organization(
        id=GENESIS_ORG_ID,
        slug="xiosync-system",
        name="XIOSYNC System",
        state="active",
        resource_quotas={},
        created_at=_NOW,
    )
    session = _mock_session(existing_org=existing_org)
    service = BootstrapService(session)

    result = service.genesis(
        admin_email="admin@xiosync.dev",
        admin_password="any-password",
        now=_NOW,
    )

    # Verify returned result flags already_existed
    assert result.already_existed is True
    assert result.organization_id == GENESIS_ORG_ID
    assert result.system_actor_id == SYSTEM_ACTOR_ID
    assert result.human_actor_id == BOOTSTRAP_HUMAN_ACTOR_ID
    assert result.ai_agent_actor_id == AI_AGENT_ACTOR_ID
    assert result.auth_identity_id is None

    # Verify NO entities were added or flushed
    session.add.assert_not_called()
    session.add_all.assert_not_called()
    session.flush.assert_not_called()


def test_genesis_multiple_consecutive_runs_are_idempotent() -> None:
    """Verify multiple calls to genesis() consistently return identical results without mutation."""
    existing_org = Organization(
        id=GENESIS_ORG_ID,
        slug="xiosync-system",
        name="XIOSYNC System",
        state="active",
        resource_quotas={},
        created_at=_NOW,
    )
    session = _mock_session(existing_org=existing_org)
    service = BootstrapService(session)

    results = [service.genesis(now=_NOW) for _ in range(3)]

    for res in results:
        assert res.already_existed is True
        assert res.organization_id == GENESIS_ORG_ID
        assert res.system_actor_id == SYSTEM_ACTOR_ID
        assert res.human_actor_id == BOOTSTRAP_HUMAN_ACTOR_ID
        assert res.ai_agent_actor_id == AI_AGENT_ACTOR_ID
        assert res.auth_identity_id is None

    session.add.assert_not_called()
    session.add_all.assert_not_called()
    session.flush.assert_not_called()
