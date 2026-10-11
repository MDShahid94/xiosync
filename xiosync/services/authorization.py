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
    ShareChecker,
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
        share_checker: ShareChecker | None = None,
    ) -> Decision:
        # Auto-build a rate checker if a rate limiter is available and
        # no explicit checker was provided.
        if rate_checker is None and self._rate_limiter is not None:
            from xiosync.core.capability_rate import build_rate_checker

            rate_checker = build_rate_checker(self._rate_limiter, context.actor_id, capability)

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
                share_checker=share_checker,
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

    def revoke_grant(
        self,
        context: OrgContext,
        *,
        grant_id: uuid.UUID,
        now: datetime,
    ) -> int:
        """Revoke a grant and all its delegation descendants via recursive CTE."""
        from sqlalchemy import text as sa_text
        result = self._repository._session.execute(
            sa_text("""
            WITH RECURSIVE delegation_tree AS (
                SELECT id, organization_id
                FROM grants
                WHERE id = :root_id
                  AND organization_id = :org_id
                  AND state = 'active'
                UNION ALL
                SELECT g.id, g.organization_id
                FROM grants g
                JOIN delegation_tree dt ON g.parent_grant_id = dt.id
                                        AND g.organization_id = dt.organization_id
                WHERE g.state = 'active'
            )
            UPDATE grants SET state = 'revoked', revoked_at = :now
            WHERE id IN (SELECT id FROM delegation_tree)
              AND state = 'active'
            """),
            {"root_id": grant_id, "org_id": context.organization_id, "now": now},
        )
        self._repository._session.flush()
        return result.rowcount

    def create_grant(
        self,
        context: OrgContext,
        *,
        actor_id: uuid.UUID,
        capability_id: uuid.UUID,
        granted_by: uuid.UUID | None = None,
        parent_grant_id: uuid.UUID | None = None,
        constraints: dict | None = None,
        expires_at: datetime | None = None,
    ) -> uuid.UUID:
        """Create a grant with provenance tracking and prerequisite enforcement."""
        from sqlalchemy import func, select
        from xiosync.persistence.models.authorization import Capability, Grant

        # Load capability to check prerequisites
        cap = self._repository._session.scalar(
            select(Capability).where(
                Capability.id == capability_id,
                Capability.organization_id == context.organization_id,
            )
        )
        if cap is None:
            raise ValueError(f"Capability {capability_id} not found")

        # Verify prerequisites are satisfied
        for prereq_id in (cap.requires or []):
            prereq_uuid = uuid.UUID(prereq_id) if isinstance(prereq_id, str) else prereq_id
            has_prereq = self._repository._session.scalar(
                select(func.count()).select_from(Grant).where(
                    Grant.organization_id == context.organization_id,
                    Grant.actor_id == actor_id,
                    Grant.capability_id == prereq_uuid,
                    Grant.state == "active",
                )
            )
            if not has_prereq:
                raise ValueError(
                    f"Prerequisite capability {prereq_id} not granted to actor {actor_id}"
                )

        grant = Grant(
            organization_id=context.organization_id,
            actor_id=actor_id,
            capability_id=capability_id,
            state="active",
            constraints=constraints or {},
            expires_at=expires_at,
            granted_by=granted_by or context.actor_id,
            parent_grant_id=parent_grant_id,
        )
        self._repository._session.add(grant)
        self._repository._session.flush()
        return grant.id
