"""System context for background workers.

Workers operate outside of a user request, so they need a synthetic
OrgContext with system-level privileges. This module provides a factory
for building such contexts.
"""

from __future__ import annotations

import uuid

from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole

# Well-known system actor UUID — used by all background worker processes.
# This is NOT a real actor in the database; it's a sentinel value that
# identifies automated system operations in audit logs.
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")
SYSTEM_AUTH_ID = uuid.UUID("00000000-0000-7000-8000-000000000002")
SYSTEM_SESSION_ID = uuid.UUID("00000000-0000-7000-8000-000000000003")


def system_context(organization_id: uuid.UUID) -> OrgContext:
    """Build a system OrgContext for background worker operations.

    Uses well-known system UUIDs and platform_admin role so that
    worker operations are not blocked by authorization checks.
    """
    return OrgContext(
        auth_identity_id=SYSTEM_AUTH_ID,
        actor_id=SYSTEM_ACTOR_ID,
        organization_id=organization_id,
        session_id=SYSTEM_SESSION_ID,
        platform_role=PlatformRole.PLATFORM_ADMIN,
        membership_role=MembershipRole.ORG_OWNER,
    )
