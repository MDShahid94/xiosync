"""Secret reference management (Gap S-1).

``SecretRefService`` manages provider-agnostic secret references. The platform
stores metadata only — actual secret values are resolved by provider adapters
at the worker side via the ``GET /execution/tasks/{id}/secrets`` endpoint.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.domain.secrets import validate_provider, validate_secret_state
from xiosync.persistence.models.secrets import SecretRef
from xiosync.platform.ids import new_id

__all__ = [
    "SecretNotFoundError",
    "SecretRefRecord",
    "SecretRefService",
]


@dataclass(frozen=True, slots=True)
class SecretRefRecord:
    """Frozen snapshot of a ``secret_refs`` row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    provider: str
    ref_config: dict[str, Any]
    state: str
    created_at: datetime
    rotated_at: datetime | None
    created_by: uuid.UUID


class SecretNotFoundError(ValueError):
    """Raised when the requested secret reference does not exist."""


def _record(row: SecretRef) -> SecretRefRecord:
    return SecretRefRecord(
        id=row.id,
        organization_id=row.organization_id,
        name=row.name,
        provider=row.provider,
        ref_config=dict(row.ref_config),
        state=row.state,
        created_at=row.created_at,
        rotated_at=row.rotated_at,
        created_by=row.created_by,
    )


class SecretRefService:
    """Use cases for provider-agnostic secret references (Gap S-1)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_secret(
        self,
        context: OrgContext,
        *,
        name: str,
        provider: str,
        ref_config: dict[str, Any],
        created_by: uuid.UUID,
    ) -> SecretRefRecord:
        """Register a new secret reference."""
        validate_provider(provider)
        ref_id = new_id()
        row = SecretRef(
            id=ref_id,
            organization_id=context.organization_id,
            name=name,
            provider=provider,
            ref_config=ref_config,
            created_by=created_by,
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def get_secret(
        self,
        context: OrgContext,
        secret_id: uuid.UUID,
    ) -> SecretRefRecord:
        """Fetch one secret reference, or raise ``SecretNotFoundError``."""
        row = self._session.scalar(
            select(SecretRef).where(
                SecretRef.organization_id == context.organization_id,
                SecretRef.id == secret_id,
            )
        )
        if row is None:
            raise SecretNotFoundError(
                f"secret {secret_id} not found in org {context.organization_id}"
            )
        return _record(row)

    def get_secret_by_name(
        self,
        context: OrgContext,
        name: str,
    ) -> SecretRefRecord | None:
        """Fetch a secret reference by name, or return ``None``."""
        row = self._session.scalar(
            select(SecretRef).where(
                SecretRef.organization_id == context.organization_id,
                SecretRef.name == name,
            )
        )
        return _record(row) if row else None

    def list_secrets(
        self,
        context: OrgContext,
        *,
        state: str | None = None,
        limit: int = 50,
    ) -> list[SecretRefRecord]:
        """List secret references for this org."""
        stmt = (
            select(SecretRef)
            .where(SecretRef.organization_id == context.organization_id)
            .order_by(SecretRef.created_at.desc())
            .limit(limit)
        )
        if state is not None:
            stmt = stmt.where(SecretRef.state == state)
        return [_record(row) for row in self._session.scalars(stmt).all()]

    def rotate_secret(
        self,
        context: OrgContext,
        secret_id: uuid.UUID,
        *,
        new_ref_config: dict[str, Any],
        now: datetime | None = None,
    ) -> SecretRefRecord:
        """Mark a secret as rotated and update its ref_config."""
        from datetime import datetime as dt, timezone

        row = self._session.scalar(
            select(SecretRef).where(
                SecretRef.organization_id == context.organization_id,
                SecretRef.id == secret_id,
            )
        )
        if row is None:
            raise SecretNotFoundError(
                f"secret {secret_id} not found in org {context.organization_id}"
            )
        row.ref_config = new_ref_config
        row.state = "active"  # Re-activate after rotation
        row.rotated_at = now or dt.now(timezone.utc)
        self._session.flush()
        return _record(row)

    def revoke_secret(
        self,
        context: OrgContext,
        secret_id: uuid.UUID,
    ) -> SecretRefRecord:
        """Revoke a secret reference."""
        row = self._session.scalar(
            select(SecretRef).where(
                SecretRef.organization_id == context.organization_id,
                SecretRef.id == secret_id,
            )
        )
        if row is None:
            raise SecretNotFoundError(
                f"secret {secret_id} not found in org {context.organization_id}"
            )
        row.state = "revoked"
        self._session.flush()
        return _record(row)

    def resolve_secrets(
        self,
        context: OrgContext,
        names: list[str],
    ) -> list[SecretRefRecord]:
        """Resolve secret references by name. Returns only active secrets."""
        stmt = (
            select(SecretRef)
            .where(
                SecretRef.organization_id == context.organization_id,
                SecretRef.name.in_(names),
                SecretRef.state == "active",
            )
        )
        return [_record(row) for row in self._session.scalars(stmt).all()]
