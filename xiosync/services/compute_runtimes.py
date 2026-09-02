"""Compute Runtime Service (abstracting runtime provisioning)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.domain.events import STATE_CHANGE
from xiosync.persistence.models.browser import (
    ComputeRuntime as RuntimeProvider,
    RuntimeNode,
)
from xiosync.platform.ids import new_id
from xiosync.services.events import EventService
from xiosync.services.operations import OperationService

__all__ = [
    "ComputeRuntimeService",
    "NodeHealthRecord",
    "RuntimeNodeRecord",
    "RuntimeProviderRecord",
]


@dataclass(frozen=True, slots=True)
class RuntimeProviderRecord:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    provider: str
    config: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RuntimeNodeRecord:
    id: uuid.UUID
    organization_id: uuid.UUID
    runtime_id: uuid.UUID
    state: str
    spec: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NodeHealthRecord:
    node_id: uuid.UUID
    status: str
    last_heartbeat: datetime | None


class ComputeRuntimeService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._event_service = EventService(session)
        # Assuming OperationService takes a repository, we might need to construct it
        # or we just write Operations directly. Let's write directly to avoid repository setup if complex.
        # Actually, let's just use raw Operation model for simplicity if we don't have the repo.
        pass

    def _record_op(
        self,
        ctx: OrgContext,
        operation: str,
        actor_id: uuid.UUID,
        from_state: str | None = None,
        to_state: str | None = None,
    ) -> uuid.UUID:
        from xiosync.persistence.models.operations import Operation
        op_id = new_id()
        self._session.add(
            Operation(
                id=op_id,
                organization_id=ctx.organization_id,
                actor_id=actor_id,
                operation=operation,
                trigger="api",
                initiated_by=actor_id,
                from_state=from_state,
                to_state=to_state,
                depth_level=0,
            )
        )
        return op_id

    def register_provider(
        self,
        ctx: OrgContext,
        *,
        name: str,
        provider: str,
        config: Mapping[str, Any],
    ) -> RuntimeProviderRecord:
        runtime_id = new_id()
        now = datetime.now(tz=UTC)
        
        row = RuntimeProvider(
            id=runtime_id,
            organization_id=ctx.organization_id,
            name=name,
            provider=provider,
            config=dict(config),
            created_at=now,
        )
        self._session.add(row)
        
        op_id = self._record_op(ctx, "register_provider", actor_id=ctx.actor_id or ctx.organization_id)
        self._event_service.append(
            ctx,
            event_type="provider.registered",
            payload={"runtime_id": str(runtime_id), "provider": provider},
            actor_id=ctx.actor_id,
            operation_id=op_id,
            entity_type="runtime_provider",
            entity_id=runtime_id,
        )
        
        self._session.flush()
        return RuntimeProviderRecord(
            id=row.id,
            organization_id=row.organization_id,
            name=row.name,
            provider=row.provider,
            config=row.config,
            created_at=row.created_at,
        )

    def provision_node(
        self,
        ctx: OrgContext,
        *,
        runtime_id: uuid.UUID,
        spec: Mapping[str, Any],
    ) -> RuntimeNodeRecord:
        node_id = new_id()
        now = datetime.now(tz=UTC)
        
        row = RuntimeNode(
            id=node_id,
            organization_id=ctx.organization_id,
            runtime_id=runtime_id,
            state="provisioning",
            node_metadata=dict(spec), hostname=f"node-{str(node_id)[:8]}",
            created_at=now,
        )
        self._session.add(row)
        
        op_id = self._record_op(
            ctx, 
            "provision_node", 
            actor_id=ctx.actor_id or ctx.organization_id,
            to_state="provisioning",
        )
        self._event_service.append_state_change(
            ctx,
            actor_id=ctx.actor_id or ctx.organization_id,
            from_state="none",
            to_state="provisioning",
            operation_id=op_id,
            trigger="api",
            entity_type="runtime_node",
        )
        
        self._session.flush()
        return RuntimeNodeRecord(
            id=row.id,
            organization_id=row.organization_id,
            runtime_id=row.runtime_id,
            state=row.state,
            spec=row.node_metadata,
            created_at=row.created_at,
        )

    def list_nodes(
        self,
        ctx: OrgContext,
        *,
        runtime_id: uuid.UUID,
        state: str | None = None,
    ) -> list[RuntimeNodeRecord]:
        stmt = select(RuntimeNode).where(
            RuntimeNode.organization_id == ctx.organization_id,
            RuntimeNode.runtime_id == runtime_id,
        )
        if state is not None:
            stmt = stmt.where(RuntimeNode.state == state)
            
        rows = self._session.scalars(stmt).all()
        return [
            RuntimeNodeRecord(
                id=r.id,
                organization_id=r.organization_id,
                runtime_id=r.runtime_id,
                state=r.state,
                spec=r.node_metadata,
                created_at=r.created_at,
            )
            for r in rows
        ]

    def terminate_node(self, ctx: OrgContext, node_id: uuid.UUID) -> None:
        row = self._session.scalar(
            select(RuntimeNode).where(
                RuntimeNode.organization_id == ctx.organization_id,
                RuntimeNode.id == node_id,
            ).with_for_update()
        )
        if not row:
            raise ValueError(f"Node {node_id} not found")
            
        old_state = row.state
        row.state = "terminated"
        
        op_id = self._record_op(
            ctx, 
            "terminate_node", 
            actor_id=ctx.actor_id or ctx.organization_id,
            from_state=old_state,
            to_state="terminated",
        )
        self._event_service.append_state_change(
            ctx,
            actor_id=ctx.actor_id or ctx.organization_id,
            from_state=old_state,
            to_state="terminated",
            operation_id=op_id,
            trigger="api",
            entity_type="runtime_node",
        )
        self._session.flush()

    def get_node_health(self, ctx: OrgContext, node_id: uuid.UUID) -> NodeHealthRecord:
        row = self._session.scalar(
            select(RuntimeNode).where(
                RuntimeNode.organization_id == ctx.organization_id,
                RuntimeNode.id == node_id,
            )
        )
        if not row:
            raise ValueError(f"Node {node_id} not found")
            
        return NodeHealthRecord(
            node_id=row.id,
            status="healthy" if row.state == "running" else "unknown",
            last_heartbeat=datetime.now(tz=UTC) if row.state == "running" else None,
        )
