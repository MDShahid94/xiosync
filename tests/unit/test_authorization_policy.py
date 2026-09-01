"""Unit tests for the fail-closed authorization decision point (doc 05 §4).

These exercise ``AuthorizationService.authorize`` — the I/O-aware wrapper in
``services/authorization.py`` — with a mocked repository so we can assert two
things the pure policy alone cannot prove end to end:

* the **normative evaluation order** of doc 05 §4 is honoured, i.e. an earlier
  failing check *short-circuits* and masks every later one, and
* a ``policy_decision`` event is **always** emitted (allow or deny), because
  the audit write is part of the decision point, not an afterthought.

Per INV-ROADMAP-3 the repository collaborator is a ``create_autospec`` mock
(typed ``Any``) rather than a hand-built ``Fake`` — we are proving the
service's control flow, not the repository's SQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import create_autospec

from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.authorization import (
    ActorRecord,
    AuthorizationRepository,
    GrantRecord,
    OrganizationRecord,
)
from xiosync.services.authorization import AuthorizationService

_NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_RESOURCE_ID = uuid.uuid4()
_CAPABILITY = "documents.read"


def _context() -> OrgContext:
    return OrgContext(
        auth_identity_id=uuid.uuid4(),
        actor_id=_ACTOR_ID,
        organization_id=_ORG_ID,
        session_id=uuid.uuid4(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_MEMBER,
    )


def _actor(
    *, state: str = "active", organization_id: uuid.UUID = _ORG_ID, trust_tier: str = "trusted"
) -> ActorRecord:
    return ActorRecord(_ACTOR_ID, organization_id, state, trust_tier)


def _organization(*, state: str = "active") -> OrganizationRecord:
    return OrganizationRecord(_ORG_ID, state)


def _grant(
    *,
    state: str = "active",
    constraints: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> GrantRecord:
    return GrantRecord(
        uuid.uuid4(), _ORG_ID, _ACTOR_ID, _CAPABILITY, state, constraints or {}, expires_at
    )


def _repository(
    *,
    actor: ActorRecord | None = None,
    organization: OrganizationRecord | None = None,
    grants: list[GrantRecord] | None = None,
) -> Any:
    """A fully stubbed authorization repository (INV-ROADMAP-3: mock, not Fake)."""
    repository = create_autospec(AuthorizationRepository, instance=True)
    repository.get_actor.return_value = actor
    repository.get_organization.return_value = organization
    repository.list_grants.return_value = [] if grants is None else grants
    repository.add_policy_event.return_value = uuid.uuid4()
    return repository


def _authorize(
    repository: Any,
    *,
    resource_organization_id: uuid.UUID = _ORG_ID,
    operation: str = "read",
    resource_type: str = "document",
    arguments: dict[str, Any] | None = None,
) -> Any:
    service = AuthorizationService(repository)
    return service.authorize(
        _context(),
        capability=_CAPABILITY,
        operation=operation,
        resource_type=resource_type,
        resource_id=_RESOURCE_ID,
        resource_organization_id=resource_organization_id,
        arguments=arguments or {},
        now=_NOW,
    )


def _emitted_payload(repository: Any) -> Any:
    """The payload handed to ``add_policy_event`` (positional: context, payload)."""
    repository.add_policy_event.assert_called_once()
    return repository.add_policy_event.call_args.args[1]


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_allows_when_every_check_passes() -> None:
    repository = _repository(
        actor=_actor(),
        organization=_organization(),
        grants=[_grant(constraints={"operations": ["read"], "resource_types": ["document"]})],
    )

    decision = _authorize(repository)

    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.grant_id is not None
    payload = _emitted_payload(repository)
    assert payload["allowed"] is True
    assert payload["reason"] == "allowed"
    assert payload["capability"] == _CAPABILITY


# --------------------------------------------------------------------------- #
# Evaluation order — doc 05 §4 (each earlier failure MUST mask later ones)
# --------------------------------------------------------------------------- #
def test_step1_inactive_actor_masks_resource_and_grant_failures() -> None:
    """Step 1 (actor) runs first: an inactive actor denies even with a good grant."""
    repository = _repository(
        actor=_actor(state="suspended"),
        organization=_organization(),
        grants=[_grant()],  # a perfectly valid grant that must NOT be reached
    )

    # Also give it a cross-org resource so a later step would also fail.
    decision = _authorize(repository, resource_organization_id=_OTHER_ORG_ID)

    assert decision.allowed is False
    assert decision.reason == "actor_invalid"


def test_step1_actor_outside_target_organization_denied() -> None:
    repository = _repository(
        actor=_actor(organization_id=_OTHER_ORG_ID),
        organization=_organization(),
        grants=[_grant()],
    )

    decision = _authorize(repository)

    assert decision.allowed is False
    assert decision.reason == "actor_invalid"


def test_step1_missing_actor_denied() -> None:
    repository = _repository(actor=None, organization=_organization(), grants=[_grant()])

    decision = _authorize(repository)

    assert decision.allowed is False
    assert decision.reason == "actor_invalid"


def test_step2_resource_ownership_masks_grant_failure() -> None:
    """Step 2 (resource) runs before step 3: a cross-org resource denies first."""
    repository = _repository(
        actor=_actor(),
        organization=_organization(),
        grants=[_grant()],  # valid grant that must NOT be reached
    )

    decision = _authorize(repository, resource_organization_id=_OTHER_ORG_ID)

    assert decision.allowed is False
    assert decision.reason == "resource_ownership_mismatch"


def test_inactive_organization_denied() -> None:
    repository = _repository(
        actor=_actor(), organization=_organization(state="suspended"), grants=[_grant()]
    )

    decision = _authorize(repository)

    assert decision.allowed is False
    assert decision.reason == "organization_inactive"


def test_step3_missing_grant_denied() -> None:
    repository = _repository(actor=_actor(), organization=_organization(), grants=[])

    decision = _authorize(repository)

    assert decision.allowed is False
    assert decision.reason == "grant_missing"


def test_step3_expired_grant_is_not_a_candidate() -> None:
    repository = _repository(
        actor=_actor(),
        organization=_organization(),
        grants=[_grant(expires_at=_NOW - timedelta(seconds=1))],
    )

    decision = _authorize(repository)

    assert decision.allowed is False
    assert decision.reason == "grant_missing"


def test_step3_revoked_grant_is_not_a_candidate() -> None:
    repository = _repository(
        actor=_actor(), organization=_organization(), grants=[_grant(state="revoked")]
    )

    decision = _authorize(repository)

    assert decision.allowed is False
    assert decision.reason == "grant_missing"


def test_step4_constraints_unsatisfied_denied() -> None:
    """Step 4 (constraints) runs last: a matched grant can still fail its rules."""
    repository = _repository(
        actor=_actor(),
        organization=_organization(),
        grants=[_grant(constraints={"operations": ["write"]})],  # request is "read"
    )

    decision = _authorize(repository, operation="read")

    assert decision.allowed is False
    assert decision.reason == "constraints_unsatisfied"


# --------------------------------------------------------------------------- #
# Mandatory audit emission (allow AND deny)
# --------------------------------------------------------------------------- #
def test_policy_decision_event_emitted_on_denial() -> None:
    repository = _repository(actor=_actor(), organization=_organization(), grants=[])

    decision = _authorize(repository)

    assert decision.allowed is False
    payload = _emitted_payload(repository)
    assert payload["allowed"] is False
    assert payload["reason"] == "grant_missing"
    assert payload["actor_id"] == str(_ACTOR_ID)
    assert payload["requested_organization_id"] == str(_ORG_ID)
    assert payload["resource_id"] == str(_RESOURCE_ID)
    assert payload["grant_id"] is None


def test_payload_carries_the_granted_grant_id_on_allow() -> None:
    repository = _repository(actor=_actor(), organization=_organization(), grants=[_grant()])

    decision = _authorize(repository)

    payload = _emitted_payload(repository)
    assert payload["grant_id"] == str(decision.grant_id)


# --------------------------------------------------------------------------- #
# Fail-closed on infrastructure errors
# --------------------------------------------------------------------------- #
def test_repository_lookup_failure_fails_closed_and_still_audits() -> None:
    repository = _repository(actor=_actor(), organization=_organization(), grants=[_grant()])
    repository.get_actor.side_effect = RuntimeError("database unavailable")

    decision = _authorize(repository)

    assert decision.allowed is False
    assert decision.reason == "authorization_lookup_failed"
    payload = _emitted_payload(repository)
    assert payload["allowed"] is False
    assert payload["reason"] == "authorization_lookup_failed"


def test_event_emission_failure_forces_denial() -> None:
    repository = _repository(actor=_actor(), organization=_organization(), grants=[_grant()])
    repository.add_policy_event.side_effect = RuntimeError("audit sink down")

    decision = _authorize(repository)

    assert decision.allowed is False
    assert decision.reason == "policy_event_emission_failed"
