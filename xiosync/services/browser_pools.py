"""Browser pool management service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from sqlalchemy import select, update, delete
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.browser import BrowserPool
from xiosync.persistence.models.operations import Operation
from xiosync.platform.ids import new_id

__all__ = [
    "BrowserPoolNotFoundError",
    "BrowserPoolRecord",
    "BrowserPoolService",
]


@dataclass(frozen=True, slots=True)
class BrowserPoolRecord:
    """Frozen snapshot of a browser_pools row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    engine_type: str
    max_instances: int
    stealth_config: dict[str, Any]
    state: str
    created_at: datetime


class BrowserPoolNotFoundError(ValueError):
    """Raised when the requested browser pool does not exist in the org."""

    def __init__(self, pool_id: uuid.UUID) -> None:
        super().__init__(f"Browser pool {pool_id} not found")


def _record(row: BrowserPool) -> BrowserPoolRecord:
    return BrowserPoolRecord(
        id=row.id,
        organization_id=row.organization_id,
        name=row.name,
        engine_type=row.engine_type,
        max_instances=row.max_instances,
        stealth_config=dict(row.stealth_config),
        state=row.state,
        created_at=row.created_at,
    )


class BrowserPoolService:
    """Enterprise browser pool management service."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_pool(
        self,
        context: OrgContext,
        *,
        name: str,
        engine_type: str,
        max_instances: int,
        config: Mapping[str, Any] | None = None,
    ) -> BrowserPoolRecord:
        """Create a new browser pool and record an audit operation."""
        if engine_type not in ("chromium", "chrome", "firefox", "webkit", "custom"):
            raise ValueError(f"Unsupported engine type: {engine_type}")

        now = datetime.now(UTC)
        pool = BrowserPool(
            id=new_id(),
            organization_id=context.organization_id,
            name=name,
            engine_type=engine_type,
            max_instances=max_instances,
            stealth_config=dict(config) if config else {},
            state="active",
            created_at=now,
        )
        self._session.add(pool)

        op_id = new_id()
        self._session.add(
            Operation(
                id=op_id,
                organization_id=context.organization_id,
                actor_id=context.actor_id,
                operation="browser.pool.create",
                trigger="user_command",
                initiated_by=context.actor_id,
                scope="organization",
                outcome="success",
                rationale=f"Created browser pool: {name}",
                started_at=now,
                completed_at=now,
            )
        )
        self._session.add(
            Event(
                id=new_id(),
                organization_id=context.organization_id,
                event_type="browser_pool.created",
                actor_id=context.actor_id,
                severity="info",
                operation_id=op_id,
                entity_type="browser_pool",
                entity_id=pool.id,
                payload={
                    "name": name,
                    "engine_type": engine_type,
                    "max_instances": max_instances,
                },
                created_at=now,
            )
        )
        self._session.flush()
        return _record(pool)

    def get_pool(
        self,
        context: OrgContext,
        pool_id: uuid.UUID,
    ) -> BrowserPoolRecord:
        """Fetch one browser pool in this org, or raise BrowserPoolNotFoundError."""
        row = self._session.scalar(
            select(BrowserPool).where(
                BrowserPool.organization_id == context.organization_id,
                BrowserPool.id == pool_id,
            )
        )
        if row is None:
            raise BrowserPoolNotFoundError(pool_id)
        return _record(row)

    def list_pools(
        self,
        context: OrgContext,
        *,
        project_id: uuid.UUID | None = None,
    ) -> list[BrowserPoolRecord]:
        """List all browser pools in this org, optionally filtered by project_id."""
        stmt = (
            select(BrowserPool)
            .where(BrowserPool.organization_id == context.organization_id)
            .order_by(BrowserPool.created_at.desc())
        )
        if project_id is not None:
            stmt = stmt.where(BrowserPool.project_id == project_id)
        rows = self._session.scalars(stmt).all()
        return [_record(row) for row in rows]

    def scale_pool(
        self,
        context: OrgContext,
        pool_id: uuid.UUID,
        *,
        target_instances: int,
    ) -> BrowserPoolRecord:
        """Scale a browser pool to a new maximum number of instances."""
        pool = self._session.scalar(
            select(BrowserPool).where(
                BrowserPool.id == pool_id,
                BrowserPool.organization_id == context.organization_id,
            )
        )
        if pool is None:
            raise BrowserPoolNotFoundError(pool_id)

        now = datetime.now(UTC)
        old_instances = pool.max_instances
        
        self._session.execute(
            update(BrowserPool)
            .where(BrowserPool.id == pool_id)
            .values(max_instances=target_instances, updated_at=now)
        )

        op_id = new_id()
        self._session.add(
            Operation(
                id=op_id,
                organization_id=context.organization_id,
                actor_id=context.actor_id,
                operation="browser.pool.scale",
                trigger="user_command",
                initiated_by=context.actor_id,
                scope="organization",
                outcome="success",
                rationale=f"Scaled browser pool {pool.name} to {target_instances} instances",
                started_at=now,
                completed_at=now,
            )
        )
        self._session.add(
            Event(
                id=new_id(),
                organization_id=context.organization_id,
                event_type="browser_pool.scaled",
                actor_id=context.actor_id,
                severity="info",
                operation_id=op_id,
                entity_type="browser_pool",
                entity_id=pool_id,
                payload={
                    "old_instances": old_instances,
                    "new_instances": target_instances,
                },
                created_at=now,
            )
        )
        self._session.flush()

        self._session.expire(pool)
        fresh_pool = self._session.scalar(
            select(BrowserPool).where(BrowserPool.id == pool_id)
        )
        return _record(fresh_pool)  # type: ignore

    def destroy_pool(
        self,
        context: OrgContext,
        pool_id: uuid.UUID,
    ) -> None:
        """Destroy a browser pool and record an audit operation."""
        pool = self._session.scalar(
            select(BrowserPool).where(
                BrowserPool.id == pool_id,
                BrowserPool.organization_id == context.organization_id,
            )
        )
        if pool is None:
            raise BrowserPoolNotFoundError(pool_id)

        now = datetime.now(UTC)
        
        self._session.execute(
            delete(BrowserPool).where(BrowserPool.id == pool_id)
        )

        op_id = new_id()
        self._session.add(
            Operation(
                id=op_id,
                organization_id=context.organization_id,
                actor_id=context.actor_id,
                operation="browser.pool.destroy",
                trigger="user_command",
                initiated_by=context.actor_id,
                scope="organization",
                outcome="success",
                rationale=f"Destroyed browser pool: {pool.name}",
                started_at=now,
                completed_at=now,
            )
        )
        self._session.add(
            Event(
                id=new_id(),
                organization_id=context.organization_id,
                event_type="browser_pool.destroyed",
                actor_id=context.actor_id,
                severity="warning",
                operation_id=op_id,
                entity_type="browser_pool",
                entity_id=pool_id,
                payload={"name": pool.name},
                created_at=now,
            )
        )
        self._session.flush()
