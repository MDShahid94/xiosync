"""Tenant-scoped persistence for Operations (docs 03 §2.6, 06 §5).

Every method takes ``OrgContext`` first (INV-TENANT-3) and every query filters
by ``context.organization_id``; the RLS GUC bound by ``org_scoped_session`` is
the backstop (doc 05 §3.2). The repository returns frozen record dataclasses,
never live ORM rows, so the service layer holds no session state.

The composite ``(organization_id, parent_operation_id)`` FK on ``operations``
guarantees same-org parenting (INV-OP-2) at the schema; this module additionally
materializes the org's parent map so the service can validate acyclicity
(INV-OP-1) *before* the write.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.operations import Operation
from xiosync.platform.ids import new_id


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """One ``operations`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    actor_id: uuid.UUID
    operation: str
    trigger: str
    initiated_by: uuid.UUID
    from_state: str | None
    to_state: str | None
    scope: str | None
    depth_level: int
    parent_operation_id: uuid.UUID | None
    outcome: str | None
    started_at: datetime


def _record(row: Operation) -> OperationRecord:
    return OperationRecord(
        id=row.id,
        organization_id=row.organization_id,
        actor_id=row.actor_id,
        operation=row.operation,
        trigger=row.trigger,
        initiated_by=row.initiated_by,
        from_state=row.from_state,
        to_state=row.to_state,
        scope=row.scope,
        depth_level=row.depth_level,
        parent_operation_id=row.parent_operation_id,
        outcome=row.outcome,
        started_at=row.started_at,
    )


class OperationRepository:
    """All database access for Operations within one tenant scope."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_operation(self, context: OrgContext, operation_id: uuid.UUID) -> OperationRecord | None:
        row = self._session.scalar(
            select(Operation).where(
                Operation.organization_id == context.organization_id,
                Operation.id == operation_id,
            )
        )
        return None if row is None else _record(row)

    def load_parent_map(self, context: OrgContext) -> dict[uuid.UUID, uuid.UUID | None]:
        """Map every operation in the org to its parent (``None`` for roots).

        The hierarchy is materialized per organization so the acyclicity check
        (INV-OP-1) never leaves the tenant scope.
        """
        rows = self._session.execute(
            select(Operation.id, Operation.parent_operation_id).where(
                Operation.organization_id == context.organization_id
            )
        ).all()
        return {row_id: parent_id for row_id, parent_id in rows}

    def insert_operation(
        self,
        context: OrgContext,
        *,
        actor_id: uuid.UUID,
        operation: str,
        trigger: str,
        initiated_by: uuid.UUID,
        from_state: str | None,
        to_state: str | None,
        collaborators: list[dict[str, Any]],
        scope: str | None,
        depth_level: int,
        parent_operation_id: uuid.UUID | None,
        artifacts: dict[str, Any] | None,
        rationale: str | None,
        outcome: str | None,
    ) -> uuid.UUID:
        operation_id = new_id()
        self._session.add(
            Operation(
                id=operation_id,
                organization_id=context.organization_id,
                actor_id=actor_id,
                operation=operation,
                trigger=trigger,
                initiated_by=initiated_by,
                from_state=from_state,
                to_state=to_state,
                collaborators=collaborators,
                scope=scope,
                depth_level=depth_level,
                parent_operation_id=parent_operation_id,
                artifacts=artifacts,
                rationale=rationale,
                outcome=outcome,
            )
        )
        self._session.flush()
        return operation_id

    def set_parent(
        self,
        context: OrgContext,
        operation_id: uuid.UUID,
        *,
        parent_operation_id: uuid.UUID | None,
        depth_level: int,
    ) -> bool:
        """Reparent an operation; returns whether a row was updated."""
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(Operation)
                .where(
                    Operation.organization_id == context.organization_id,
                    Operation.id == operation_id,
                )
                .values(parent_operation_id=parent_operation_id, depth_level=depth_level)
            ),
        )
        self._session.flush()
        return result.rowcount == 1
