"""I/O-aware authorization decision point with mandatory audit emission."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from xiosync.domain.authorization import (
    Actor,
    Decision,
    Grant,
    Organization,
    RateChecker,
    Resource,
    authorize,
)
from xiosync.domain.context import OrgContext
from xiosync.persistence.authorization import AuthorizationRepository


class AuthorizationService:
    def __init__(
        self,
        repository: AuthorizationRepository,
        rate_limiter: Any | None = None,
    ) -> None:
        self._repository = repository
        self._rate_limiter = rate_limiter

    def authorize(
        self,
        context: OrgContext,
        *,
        capability: str,
        operation: str,
        resource_type: str,
        resource_id: uuid.UUID,
        resource_organization_id: uuid.UUID,
        arguments: Mapping[str, Any],
        now: datetime,
        rate_checker: RateChecker | None = None,
    ) -> Decision:
        # Auto-build a rate checker if a rate limiter is available and
        # no explicit checker was provided.
        if rate_checker is None and self._rate_limiter is not None:
            from xiosync.core.capability_rate import build_rate_checker

            rate_checker = build_rate_checker(
                self._rate_limiter, context.actor_id, capability
            )

        decision: Decision
        try:
            actor_row = self._repository.get_actor(context)
            organization_row = self._repository.get_organization(context)
            grant_rows = self._repository.list_grants(context, capability)
            actor = (
                None
                if actor_row is None
                else Actor(
                    actor_row.id, actor_row.organization_id, actor_row.state, actor_row.trust_tier
                )
            )
            organization = (
                None
                if organization_row is None
                else Organization(organization_row.id, organization_row.state)
            )
            grants = [
                Grant(
                    row.id,
                    row.organization_id,
                    row.actor_id,
                    row.capability,
                    row.state,
                    row.constraints,
                    row.expires_at,
                )
                for row in grant_rows
            ]
            decision = authorize(
                requested_organization_id=context.organization_id,
                actor=actor,
                organization=organization,
                resource=Resource(resource_type, resource_id, resource_organization_id),
                capability=capability,
                operation=operation,
                grants=grants,
                arguments=arguments,
                now=now,
                rate_checker=rate_checker,
            )
        except Exception:
            decision = Decision(False, uuid.uuid4(), "authorization_lookup_failed")
        payload = {
            "decision_id": str(decision.decision_id),
            "allowed": decision.allowed,
            "reason": decision.reason,
            "actor_id": str(context.actor_id),
            "requested_organization_id": str(context.organization_id),
            "capability": capability,
            "operation": operation,
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "resource_organization_id": str(resource_organization_id),
            "grant_id": None if decision.grant_id is None else str(decision.grant_id),
        }
        try:
            self._repository.add_policy_event(context, payload)
        except Exception:
            return Decision(
                False, decision.decision_id, "policy_event_emission_failed", decision.grant_id
            )
        return decision

