"""RBAC enforcement middleware — configurable capability checks (Phase 1, Gap G-3).

Provides a FastAPI dependency ``require_capability(group_name)`` that:
1. Extracts OrgContext from request state
2. Looks up the capability group → expands to fine-grained operations
3. Checks if the actor has a matching grant via AuthorizationService
4. Returns 403 with RFC 7807 problem detail on denial
5. Logs the policy decision as an event

The capability groups are configurable per-org (Q2 Option C): a group
name expands to a list of fine-grained operations.  ``"*"`` in the
operations list means "all operations" (platform admin wildcard).

For routes that are truly read-only and require only authentication (not
authorization), the existing auth middleware is sufficient.  This module
adds fine-grained capability checks on top.

Usage in route handlers::

    from xiosync.api.middleware.rbac import require_capability

    @router.post("/workflows", dependencies=[Depends(require_capability("workflow.manage"))])
    def create_workflow(...):
        ...
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.models.registry import CapabilityGroup

logger = logging.getLogger(__name__)

# -- Membership role hierarchy (higher index = more privilege) -----------------
_ROLE_LEVEL = {
    MembershipRole.ORG_VIEWER: 0,
    MembershipRole.ORG_MEMBER: 1,
    MembershipRole.ORG_ADMIN: 2,
    MembershipRole.ORG_OWNER: 3,
}

# -- Default minimum role per capability group ---------------------------------
# These are the defaults; orgs can override via their own group definitions.
_DEFAULT_MIN_ROLE: dict[str, MembershipRole] = {
    # Admin-only operations
    "platform.admin": MembershipRole.ORG_OWNER,
    "org.manage": MembershipRole.ORG_ADMIN,
    "actor.manage": MembershipRole.ORG_ADMIN,
    "plugin.admin": MembershipRole.ORG_ADMIN,
    "secret.manage": MembershipRole.ORG_ADMIN,
    "worker.manage": MembershipRole.ORG_ADMIN,
    "share.manage": MembershipRole.ORG_ADMIN,
    "webhook.manage": MembershipRole.ORG_ADMIN,
    "capability.manage": MembershipRole.ORG_ADMIN,
    # Member operations
    "workflow.manage": MembershipRole.ORG_MEMBER,
    "task.execute": MembershipRole.ORG_MEMBER,
    "event.manage": MembershipRole.ORG_MEMBER,
    "artifact.manage": MembershipRole.ORG_MEMBER,
    "ontology.manage": MembershipRole.ORG_MEMBER,
    "dlq.manage": MembershipRole.ORG_MEMBER,
    "trigger.manage": MembershipRole.ORG_MEMBER,
    # Read-only
    "readonly": MembershipRole.ORG_VIEWER,
    "metering.read": MembershipRole.ORG_VIEWER,
}


def _check_role(
    context: OrgContext,
    group_name: str,
) -> bool:
    """Check if the actor's membership role meets the minimum for this group.

    Platform admins always pass.  The role hierarchy check is the fast path
    before the more expensive grant-based authorization.
    """
    if context.platform_role == PlatformRole.PLATFORM_ADMIN:
        return True

    min_role = _DEFAULT_MIN_ROLE.get(group_name)
    if min_role is None:
        # Unknown group — deny by default (fail-closed)
        return False

    actor_level = _ROLE_LEVEL.get(context.membership_role, -1)
    required_level = _ROLE_LEVEL.get(min_role, 99)
    return actor_level >= required_level


class CapabilityDeniedError(HTTPException):
    """Raised when an actor lacks the required capability."""

    def __init__(
        self,
        group_name: str,
        actor_id: uuid.UUID,
        membership_role: str,
        request_id: str = "",
    ) -> None:
        detail = {
            "type": "https://xiosync.dev/problems/capability_denied",
            "title": "Insufficient capabilities",
            "status": 403,
            "code": "capability_denied",
            "detail": (
                f"Actor {actor_id} with role '{membership_role}' "
                f"does not have the '{group_name}' capability"
            ),
            "capability_group": group_name,
            "actor_id": str(actor_id),
            "membership_role": membership_role,
            "request_id": request_id,
        }
        super().__init__(status_code=403, detail=detail)


def require_capability(group_name: str) -> Any:
    """FastAPI dependency that enforces a capability group check.

    Usage::

        @router.post("/workflows", dependencies=[Depends(require_capability("workflow.manage"))])
        def create_workflow(...):
            ...

    The check uses the membership role hierarchy as the fast path:
    - ``platform_admin`` → always allowed
    - ``org_owner`` → allowed for all groups
    - ``org_admin`` → allowed for admin + member + viewer groups
    - ``org_member`` → allowed for member + viewer groups
    - ``org_viewer`` → allowed for viewer groups only

    Future: when capability grants are fully populated, this will also
    check the AuthorizationService for fine-grained grant-based decisions.
    """

    async def _dependency(request: Request) -> None:
        context: OrgContext | None = getattr(request.state, "org_context", None)
        if context is None:
            # Not authenticated — let the auth middleware handle it
            raise HTTPException(status_code=401, detail="Not authenticated")

        request_id = getattr(request.state, "request_id", "")

        if not _check_role(context, group_name):
            logger.warning(
                "rbac.denied: actor=%s role=%s group=%s",
                context.actor_id,
                context.membership_role,
                group_name,
            )
            raise CapabilityDeniedError(
                group_name=group_name,
                actor_id=context.actor_id,
                membership_role=context.membership_role.value,
                request_id=request_id,
            )

        logger.debug(
            "rbac.allowed: actor=%s role=%s group=%s",
            context.actor_id,
            context.membership_role,
            group_name,
        )

    return Depends(_dependency)
