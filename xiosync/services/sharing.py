"""Cross-organization resource sharing (Gap M-2).

``SharingService`` manages resource shares between organizations. Sharing is
gated by the ``XIOSYNC_ENABLE_CROSS_ORG_SHARING`` config flag (default: false).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xiosync.domain.authorization import ShareChecker

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.sharing import ResourceShare
from xiosync.platform.ids import new_id

__all__ = [
    "ShareNotFoundError",
    "ShareRecord",
    "SharingDisabledError",
    "SharingService",
    "build_share_checker",
]

SHAREABLE_TYPES = frozenset({"capability", "artifact", "workflow", "plugin"})
SHARE_PERMISSIONS = frozenset({"read", "execute", "fork"})


@dataclass(frozen=True, slots=True)
class ShareRecord:
    """Frozen snapshot of a ``resource_shares`` row."""

    id: uuid.UUID
    source_org_id: uuid.UUID
    target_org_id: uuid.UUID | None
    resource_type: str
    resource_id: uuid.UUID
    permissions: list[str]
    state: str
    created_at: datetime
    expires_at: datetime | None
    parent_share_id: uuid.UUID | None = None


class ShareNotFoundError(ValueError):
    """Raised when the requested share does not exist."""


class SharingDisabledError(ValueError):
    """Raised when cross-org sharing is not enabled."""


class InvalidShareTypeError(ValueError):
    """Raised when the resource type is not shareable."""


def _record(row: ResourceShare) -> ShareRecord:
    return ShareRecord(
        id=row.id,
        source_org_id=row.source_org_id,
        target_org_id=row.target_org_id,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        permissions=list(row.permissions),
        state=row.state,
        created_at=row.created_at,
        expires_at=row.expires_at,
        parent_share_id=row.parent_share_id,
    )


class SharingService:
    """Use cases for cross-organization resource sharing (Gap M-2)."""

    def __init__(self, session: Session, *, enabled: bool = False) -> None:
        self._session = session
        self._enabled = enabled

    def _check_enabled(self) -> None:
        if not self._enabled:
            raise SharingDisabledError(
                "cross-org sharing is disabled; set XIOSYNC_ENABLE_CROSS_ORG_SHARING=true"
            )

    def create_share(
        self,
        context: OrgContext,
        *,
        resource_type: str,
        resource_id: uuid.UUID,
        target_org_id: uuid.UUID | None = None,
        permissions: list[str] | None = None,
        expires_at: datetime | None = None,
        parent_share_id: uuid.UUID | None = None,
    ) -> ShareRecord:
        """Share a resource with another organization (or publicly)."""
        self._check_enabled()
        if resource_type not in SHAREABLE_TYPES:
            raise InvalidShareTypeError(
                f"resource type {resource_type!r} is not shareable; "
                f"expected one of {sorted(SHAREABLE_TYPES)}"
            )

        perms = permissions or ["read"]
        from xiosync.domain.sharing import validate_share_permissions

        validate_share_permissions(perms)
        share_id = new_id()
        row = ResourceShare(
            id=share_id,
            source_org_id=context.organization_id,
            target_org_id=target_org_id,
            resource_type=resource_type,
            resource_id=resource_id,
            permissions=perms,
            expires_at=expires_at,
            parent_share_id=parent_share_id,
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def revoke_share(
        self,
        context: OrgContext,
        share_id: uuid.UUID,
    ) -> ShareRecord:
        """Revoke a share and all its delegation descendants."""
        from sqlalchemy import text

        # Cascade revocation via recursive CTE
        result = self._session.execute(
            text("""
            WITH RECURSIVE share_tree AS (
                SELECT id
                FROM resource_shares
                WHERE id = :root_id
                  AND source_org_id = :org_id
                  AND state = 'active'
                UNION ALL
                SELECT rs.id
                FROM resource_shares rs
                JOIN share_tree st ON rs.parent_share_id = st.id
                WHERE rs.state = 'active'
            )
            UPDATE resource_shares SET state = 'revoked'
            WHERE id IN (SELECT id FROM share_tree)
              AND state = 'active'
            RETURNING id
            """),
            {"root_id": share_id, "org_id": context.organization_id},
        )
        revoked_ids = [row[0] for row in result.fetchall()]
        self._session.flush()

        if not revoked_ids:
            from xiosync.services.sharing import ShareNotFoundError
            raise ShareNotFoundError(share_id)

        # Return the root share record
        row = self._session.get(ResourceShare, share_id)
        return _record(row)

    def list_shares(
        self,
        context: OrgContext,
        *,
        resource_type: str | None = None,
        limit: int = 50,
    ) -> list[ShareRecord]:
        """List resources shared BY this org."""
        stmt = (
            select(ResourceShare)
            .where(ResourceShare.source_org_id == context.organization_id)
            .order_by(ResourceShare.created_at.desc())
            .limit(limit)
        )
        if resource_type is not None:
            stmt = stmt.where(ResourceShare.resource_type == resource_type)
        return [_record(row) for row in self._session.scalars(stmt).all()]

    def list_shared_with_me(
        self,
        context: OrgContext,
        *,
        resource_type: str | None = None,
        limit: int = 50,
    ) -> list[ShareRecord]:
        """List resources shared TO this org (including public shares)."""
        stmt = (
            select(ResourceShare)
            .where(
                ResourceShare.state == "active",
                (
                    (ResourceShare.target_org_id == context.organization_id)
                    | (ResourceShare.target_org_id.is_(None))
                ),
            )
            .order_by(ResourceShare.created_at.desc())
            .limit(limit)
        )
        if resource_type is not None:
            stmt = stmt.where(ResourceShare.resource_type == resource_type)
        return [_record(row) for row in self._session.scalars(stmt).all()]


def build_share_checker(session: Session) -> ShareChecker:
    """Build a ShareChecker that enforces granular permissions via JSONB containment."""
    from sqlalchemy import func, or_, select
    from xiosync.persistence.models.sharing import ResourceShare

    def check(
        resource_type: str,
        resource_id: uuid.UUID,
        requesting_org_id: uuid.UUID,
        required_permission: str,
    ) -> bool:
        return (
            session.scalar(
                select(func.count())
                .select_from(ResourceShare)
                .where(
                    ResourceShare.resource_type == resource_type,
                    ResourceShare.resource_id == resource_id,
                    ResourceShare.state == "active",
                    or_(
                        ResourceShare.target_org_id == requesting_org_id,
                        ResourceShare.target_org_id.is_(None),
                    ),
                    or_(
                        ResourceShare.expires_at.is_(None),
                        ResourceShare.expires_at > func.now(),
                    ),
                    ResourceShare.permissions.contains([required_permission]),
                )
            )
            or 0
        ) > 0

    return check
