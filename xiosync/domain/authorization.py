"""Pure, fail-closed capability authorization policy (doc 05 §4)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

TRUST_ORDER = {"newcomer": 0, "contributor": 1, "trusted": 2, "core": 3, "admin": 4}

# --- Gap S-3: Extensible constraint evaluator registry -----------------------

#: Type for a constraint evaluator function. Receives the constraint value,
#: grant, actor, resource, operation, arguments, now, and rate_checker.
#: Returns True if the constraint is satisfied, False otherwise.
ConstraintEvaluator = Callable[
    [Any, "Grant", "Actor", "Resource", str, Mapping[str, Any], datetime, "RateChecker | None"],
    bool,
]

# The registry of constraint evaluators, keyed by constraint name.
_CONSTRAINT_REGISTRY: dict[str, ConstraintEvaluator] = {}


def register_constraint_evaluator(name: str, evaluator: ConstraintEvaluator) -> None:
    """Register a custom constraint evaluator (Gap S-3).

    Allows organizations and plugins to extend the authorization system with
    custom constraint types. The evaluator function receives the constraint
    value and authorization context, and returns True if satisfied.
    """
    _CONSTRAINT_REGISTRY[name] = evaluator


def unregister_constraint_evaluator(name: str) -> None:
    """Remove a custom constraint evaluator."""
    _CONSTRAINT_REGISTRY.pop(name, None)


def get_supported_constraints() -> frozenset[str]:
    """Return the current set of supported constraint names (dynamic)."""
    return frozenset(_CONSTRAINT_REGISTRY.keys())


# --- Built-in constraint evaluators ------------------------------------------


def _eval_operations(
    value: Any, grant: "Grant", actor: "Actor", resource: "Resource",
    operation: str, arguments: Mapping[str, Any], now: datetime,
    rate_checker: "RateChecker | None",
) -> bool:
    return isinstance(value, list) and operation in value


def _eval_resource_types(
    value: Any, grant: "Grant", actor: "Actor", resource: "Resource",
    operation: str, arguments: Mapping[str, Any], now: datetime,
    rate_checker: "RateChecker | None",
) -> bool:
    return isinstance(value, list) and resource.type in value


def _eval_resource_ids(
    value: Any, grant: "Grant", actor: "Actor", resource: "Resource",
    operation: str, arguments: Mapping[str, Any], now: datetime,
    rate_checker: "RateChecker | None",
) -> bool:
    return isinstance(value, list) and str(resource.id) in value


def _eval_minimum_trust_tier(
    value: Any, grant: "Grant", actor: "Actor", resource: "Resource",
    operation: str, arguments: Mapping[str, Any], now: datetime,
    rate_checker: "RateChecker | None",
) -> bool:
    if value not in TRUST_ORDER or actor.trust_tier not in TRUST_ORDER:
        return False
    return TRUST_ORDER[actor.trust_tier] >= TRUST_ORDER[value]


def _eval_not_before(
    value: Any, grant: "Grant", actor: "Actor", resource: "Resource",
    operation: str, arguments: Mapping[str, Any], now: datetime,
    rate_checker: "RateChecker | None",
) -> bool:
    try:
        return now >= datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False


def _eval_not_after(
    value: Any, grant: "Grant", actor: "Actor", resource: "Resource",
    operation: str, arguments: Mapping[str, Any], now: datetime,
    rate_checker: "RateChecker | None",
) -> bool:
    try:
        return now < datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False


def _eval_arguments(
    value: Any, grant: "Grant", actor: "Actor", resource: "Resource",
    operation: str, arguments: Mapping[str, Any], now: datetime,
    rate_checker: "RateChecker | None",
) -> bool:
    if not isinstance(value, Mapping):
        return False
    for name, allowed in value.items():
        if not isinstance(allowed, list) or name not in arguments or arguments[name] not in allowed:
            return False
    return True


def _eval_rate(
    value: Any, grant: "Grant", actor: "Actor", resource: "Resource",
    operation: str, arguments: Mapping[str, Any], now: datetime,
    rate_checker: "RateChecker | None",
) -> bool:
    if not isinstance(value, Mapping) or rate_checker is None:
        return False
    return rate_checker(value)


# Register all built-in evaluators.
for _name, _fn in [
    ("operations", _eval_operations),
    ("resource_types", _eval_resource_types),
    ("resource_ids", _eval_resource_ids),
    ("minimum_trust_tier", _eval_minimum_trust_tier),
    ("not_before", _eval_not_before),
    ("not_after", _eval_not_after),
    ("arguments", _eval_arguments),
    ("rate", _eval_rate),
]:
    register_constraint_evaluator(_name, _fn)

# Legacy constant for backward compatibility.
SUPPORTED_CONSTRAINTS = get_supported_constraints()


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID
    organization_id: uuid.UUID
    state: str
    trust_tier: str


@dataclass(frozen=True)
class Organization:
    id: uuid.UUID
    state: str


@dataclass(frozen=True)
class Resource:
    type: str
    id: uuid.UUID
    organization_id: uuid.UUID


@dataclass(frozen=True)
class Grant:
    id: uuid.UUID
    organization_id: uuid.UUID
    actor_id: uuid.UUID
    capability: str
    state: str
    constraints: Mapping[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    decision_id: uuid.UUID
    reason: str
    grant_id: uuid.UUID | None = None


RateChecker = Callable[[Mapping[str, Any]], bool]
ShareChecker = Callable[[str, uuid.UUID, uuid.UUID], bool]
"""Callable(resource_type, resource_id, requesting_org_id) -> bool.

Returns True if there is an active, non-expired resource_share granting
the requesting org access to the resource.
"""


def _deny(reason: str, grant_id: uuid.UUID | None = None) -> Decision:
    return Decision(False, uuid.uuid4(), reason, grant_id)


def _constraints_allow(
    grant: Grant,
    actor: Actor,
    resource: Resource,
    operation: str,
    arguments: Mapping[str, Any],
    now: datetime,
    rate_checker: RateChecker | None,
) -> bool:
    for constraint_name, constraint_value in grant.constraints.items():
        evaluator = _CONSTRAINT_REGISTRY.get(constraint_name)
        if evaluator is None:
            return False
        if not evaluator(
            constraint_value, grant, actor, resource, operation, arguments, now, rate_checker
        ):
            return False
    return True


def authorize(
    *,
    requested_organization_id: uuid.UUID,
    actor: Actor | None,
    organization: Organization | None,
    resource: Resource,
    capability: str,
    operation: str,
    grants: Sequence[Grant],
    arguments: Mapping[str, Any],
    now: datetime,
    rate_checker: RateChecker | None = None,
    share_checker: ShareChecker | None = None,
) -> Decision:
    """Evaluate in the normative order and deny on every unknown/error path.

    When ``share_checker`` is provided and the resource belongs to a different
    org, the checker is consulted to see if a valid ``resource_shares`` entry
    exists. If so, the authorization proceeds with the actor's own grants
    (cross-org access is governed by what the actor is allowed to do, scoped
    by the share).
    """
    try:
        if (
            actor is None
            or actor.organization_id != requested_organization_id
            or actor.state != "active"
        ):
            return _deny("actor_invalid")
        if resource.organization_id != requested_organization_id:
            # Cross-org: check if a share exists
            if share_checker is None or not share_checker(
                resource.type, resource.id, requested_organization_id
            ):
                return _deny("resource_ownership_mismatch")
            # Share exists — proceed with the actor's own grants
        if (
            organization is None
            or organization.id != requested_organization_id
            or organization.state != "active"
        ):
            return _deny("organization_inactive")
        candidates = [
            grant
            for grant in grants
            if grant.organization_id == requested_organization_id
            and grant.actor_id == actor.id
            and grant.capability == capability
            and grant.state == "active"
            and (grant.expires_at is None or grant.expires_at > now)
        ]
        if not candidates:
            return _deny("grant_missing")
        for grant in candidates:
            if _constraints_allow(grant, actor, resource, operation, arguments, now, rate_checker):
                return Decision(True, uuid.uuid4(), "allowed", grant.id)
        return _deny("constraints_unsatisfied")
    except Exception:
        return _deny("policy_evaluation_failed")
