"""OrgContext — the resolved identity/tenant context (doc 05 §3.1; C1).

INV-TENANT-1: authentication middleware resolves this context *before any
tenant-scoped logic* runs. There is deliberately no way to construct an
``OrgContext`` with a missing or placeholder organization: construction
validates every field, so holding an instance is proof that resolution
succeeded. If org cannot be resolved, the request must be rejected with
401/403 before a handler runs — never given a default context.

This module is pure domain (RULE-ARCH-1): no I/O, no framework imports.
Every tenant-touching repository method takes this context as its first,
non-defaulted parameter (RULE-ARCH-3 / INV-TENANT-3).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum


class PlatformRole(StrEnum):
    """Platform-level role — almost always ``NONE`` (doc 05 §3.3)."""

    NONE = "none"
    PLATFORM_ADMIN = "platform_admin"


class MembershipRole(StrEnum):
    """The identity's role within the resolved organization (doc 03 §5)."""

    ORG_OWNER = "org_owner"
    ORG_ADMIN = "org_admin"
    ORG_MEMBER = "org_member"
    ORG_VIEWER = "org_viewer"


class ContextResolutionError(ValueError):
    """A field required for a valid OrgContext was missing or placeholder.

    Raised at construction time — the fail-closed guarantee that no
    placeholder-org context can exist (C1).
    """


_NIL_UUID = uuid.UUID(int=0)


def _required_uuid(value: uuid.UUID, field: str) -> uuid.UUID:
    """Reject non-UUID and nil-UUID values; both would be a placeholder."""
    if not isinstance(value, uuid.UUID):
        raise ContextResolutionError(
            f"OrgContext.{field} must be a UUID, got {type(value).__name__}"
        )
    if value == _NIL_UUID:
        raise ContextResolutionError(f"OrgContext.{field} must not be the nil UUID (placeholder)")
    return value


@dataclass(frozen=True, slots=True)
class OrgContext:
    """Who is acting, in which organization, under which session.

    Immutable by construction (``frozen``); every field is validated in
    ``__post_init__`` so an invalid context cannot exist (INV-TENANT-1).
    """

    auth_identity_id: uuid.UUID
    actor_id: uuid.UUID
    organization_id: uuid.UUID
    session_id: uuid.UUID
    platform_role: PlatformRole
    membership_role: MembershipRole

    def __post_init__(self) -> None:
        _required_uuid(self.auth_identity_id, "auth_identity_id")
        _required_uuid(self.actor_id, "actor_id")
        _required_uuid(self.organization_id, "organization_id")
        _required_uuid(self.session_id, "session_id")
        if not isinstance(self.platform_role, PlatformRole):
            raise ContextResolutionError(
                f"OrgContext.platform_role must be a PlatformRole, "
                f"got {type(self.platform_role).__name__}"
            )
        if not isinstance(self.membership_role, MembershipRole):
            raise ContextResolutionError(
                f"OrgContext.membership_role must be a MembershipRole, "
                f"got {type(self.membership_role).__name__}"
            )
