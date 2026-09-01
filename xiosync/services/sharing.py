"""Cross-organization resource sharing (Gap M-2).

``SharingService`` manages resource shares between organizations. Sharing is
gated by the ``XIOSYNC_ENABLE_CROSS_ORG_SHARING`` config flag (default: false).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.sharing import ResourceShare
from xiosync.platform.ids import new_id

__all__ = [
    "ShareNotFoundError",
    "ShareRecord",
    "SharingDisabledError",
    "SharingService",
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
    ) -> ShareRecord:
        """Share a resource with another organization (or publicly)."""
        self._check_enabled()
        if resource_type not in SHAREABLE_TYPES:
            raise InvalidShareTypeError(
                f"resource type {resource_type!r} is not shareable; "
                f"expected one of {sorted(SHAREABLE_TYPES)}"
            )

        perms = permissions or ["read"]
        share_id = new_id()
        row = ResourceShare(
            id=share_id,
            source_org_id=context.organization_id,
            target_org_id=target_org_id,
            resource_type=resource_type,
            resource_id=resource_id,
            permissions=perms,
            expires_at=expires_at,
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def revoke_share(
        self,
        context: OrgContext,
        share_id: uuid.UUID,
    ) -> ShareRecord:
        """Revoke a resource share."""
        row = self._session.scalar(
            select(ResourceShare).where(
                ResourceShare.source_org_id == context.organization_id,
                ResourceShare.id == share_id,
            )
        )
        if row is None:
            raise ShareNotFoundError(
                f"share {share_id} not found for org {context.organization_id}"
            )
        row.state = "revoked"
        self._session.flush()
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
