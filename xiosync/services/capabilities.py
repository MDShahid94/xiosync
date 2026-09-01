"""Capability use cases — create, get, deprecate, and validate (Gap X-2).

``CapabilityService`` is the sanctioned entry point for the capability
lifecycle. It manages blueprint columns (input/output schemas, execution mode,
timeout, retry policy) and provides schema validation helpers that INV-EXEC-3
can use for task input/result validation.

The caller owns the transaction (via ``org_scoped_session``); every write
flushes within it. Reads return frozen ``CapabilityRecord`` values.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.capabilities import (
    validate_capability_state,
    validate_execution_mode,
)
from xiosync.domain.context import OrgContext
from xiosync.persistence.models.authorization import Capability
from xiosync.platform.ids import new_id

__all__ = [
    "CapabilityNotFoundError",
    "CapabilityRecord",
    "CapabilityService",
]


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    """Frozen snapshot of a ``capabilities`` row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str | None
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    execution_mode: str
    timeout_ms: int | None
    retry_policy: dict[str, Any] | None
    version: int
    state: str
    created_at: datetime


class CapabilityNotFoundError(ValueError):
    """Raised when the requested capability does not exist in the org."""


def _record(row: Capability) -> CapabilityRecord:
    return CapabilityRecord(
        id=row.id,
        organization_id=row.organization_id,
        name=row.name,
        description=row.description,
        input_schema=dict(row.input_schema) if row.input_schema else None,
        output_schema=dict(row.output_schema) if row.output_schema else None,
        execution_mode=row.execution_mode,
        timeout_ms=row.timeout_ms,
        retry_policy=dict(row.retry_policy) if row.retry_policy else None,
        version=row.version,
        state=row.state,
        created_at=row.created_at,
    )


class CapabilityService:
    """Use cases for capability lifecycle (Gap X-2)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_capability(
        self,
        context: OrgContext,
        *,
        name: str,
        description: str | None = None,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        execution_mode: str = "sync",
        timeout_ms: int | None = None,
        retry_policy: dict[str, Any] | None = None,
        state: str = "active",
    ) -> CapabilityRecord:
        """Register a new capability in the org."""
        validate_execution_mode(execution_mode)
        validate_capability_state(state)
        if timeout_ms is not None and timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        capability_id = new_id()
        row = Capability(
            id=capability_id,
            organization_id=context.organization_id,
            name=name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            execution_mode=execution_mode,
            timeout_ms=timeout_ms,
            retry_policy=retry_policy,
            state=state,
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def get_capability(
        self,
        context: OrgContext,
        capability_id: uuid.UUID,
    ) -> CapabilityRecord:
        """Fetch one capability in this org, or raise ``CapabilityNotFoundError``."""
        row = self._session.scalar(
            select(Capability).where(
                Capability.organization_id == context.organization_id,
                Capability.id == capability_id,
            )
        )
        if row is None:
            raise CapabilityNotFoundError(
                f"capability {capability_id} not found in org {context.organization_id}"
            )
        return _record(row)

    def get_capability_by_name(
        self,
        context: OrgContext,
        name: str,
    ) -> CapabilityRecord | None:
        """Fetch a capability by name in this org, or ``None``."""
        row = self._session.scalar(
            select(Capability).where(
                Capability.organization_id == context.organization_id,
                Capability.name == name,
            )
        )
        return _record(row) if row else None

    def deprecate_capability(
        self,
        context: OrgContext,
        capability_id: uuid.UUID,
    ) -> CapabilityRecord:
        """Transition a capability to ``deprecated`` state.

        Deprecated capabilities remain referenceable by existing tasks but
        should not be used for new task creation.
        """
        row = self._session.scalar(
            select(Capability).where(
                Capability.organization_id == context.organization_id,
                Capability.id == capability_id,
            )
        )
        if row is None:
            raise CapabilityNotFoundError(
                f"capability {capability_id} not found in org {context.organization_id}"
            )
        row.state = "deprecated"
        self._session.flush()
        return _record(row)

    def list_capabilities(
        self,
        context: OrgContext,
        *,
        state: str | None = None,
        limit: int = 50,
    ) -> list[CapabilityRecord]:
        """List capabilities in this org with optional state filter."""
        stmt = (
            select(Capability)
            .where(Capability.organization_id == context.organization_id)
            .order_by(Capability.created_at.desc())
            .limit(limit)
        )
        if state is not None:
            validate_capability_state(state)
            stmt = stmt.where(Capability.state == state)
        rows = self._session.scalars(stmt).all()
        return [_record(row) for row in rows]
