"""Artifact use cases — create, get, and list provider-agnostic references (Gap D-1).

``ArtifactService`` is the sanctioned entry point for artifact reference
management. The platform stores only metadata (URI, provider, content type,
size, checksum); it never proxies raw bytes. Users choose their own storage
backend.

The caller owns the transaction (via ``org_scoped_session``); every write
flushes within it. Reads return frozen ``ArtifactRecord`` values.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.artifacts import validate_provider_type
from xiosync.domain.context import OrgContext
from xiosync.persistence.models.artifacts import Artifact
from xiosync.platform.ids import new_id

__all__ = [
    "ArtifactNotFoundError",
    "ArtifactRecord",
    "ArtifactService",
]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Frozen snapshot of an ``artifacts`` row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    provider_type: str
    uri: str
    content_type: str | None
    size_bytes: int | None
    checksum: str | None
    metadata: dict[str, Any]
    created_by: uuid.UUID
    created_at: datetime


class ArtifactNotFoundError(ValueError):
    """Raised when the requested artifact does not exist in the org."""


def _record(row: Artifact) -> ArtifactRecord:
    return ArtifactRecord(
        id=row.id,
        organization_id=row.organization_id,
        provider_type=row.provider_type,
        uri=row.uri,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        checksum=row.checksum,
        metadata=dict(row.extra_metadata),
        created_by=row.created_by,
        created_at=row.created_at,
    )


class ArtifactService:
    """Use cases for provider-agnostic artifact references (Gap D-1)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_artifact(
        self,
        context: OrgContext,
        *,
        provider_type: str,
        uri: str,
        created_by: uuid.UUID,
        content_type: str | None = None,
        size_bytes: int | None = None,
        checksum: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        """Register a new artifact reference in the org.

        The platform validates ``provider_type`` against the known set but does
        not validate the URI or attempt to access the storage backend — that is
        the user's responsibility.
        """
        validate_provider_type(provider_type)
        artifact_id = new_id()
        row = Artifact(
            id=artifact_id,
            organization_id=context.organization_id,
            provider_type=provider_type,
            uri=uri,
            content_type=content_type,
            size_bytes=size_bytes,
            checksum=checksum,
            extra_metadata=metadata or {},
            created_by=created_by,
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def get_artifact(
        self,
        context: OrgContext,
        artifact_id: uuid.UUID,
    ) -> ArtifactRecord:
        """Fetch one artifact in this org, or raise ``ArtifactNotFoundError``."""
        row = self._session.scalar(
            select(Artifact).where(
                Artifact.organization_id == context.organization_id,
                Artifact.id == artifact_id,
            )
        )
        if row is None:
            raise ArtifactNotFoundError(
                f"artifact {artifact_id} not found in org {context.organization_id}"
            )
        return _record(row)

    def list_artifacts(
        self,
        context: OrgContext,
        *,
        provider_type: str | None = None,
        content_type: str | None = None,
        limit: int = 50,
    ) -> list[ArtifactRecord]:
        """List artifacts in this org with optional filters."""
        stmt = (
            select(Artifact)
            .where(Artifact.organization_id == context.organization_id)
            .order_by(Artifact.created_at.desc())
            .limit(limit)
        )
        if provider_type is not None:
            stmt = stmt.where(Artifact.provider_type == provider_type)
        if content_type is not None:
            stmt = stmt.where(Artifact.content_type == content_type)
        rows = self._session.scalars(stmt).all()
        return [_record(row) for row in rows]
