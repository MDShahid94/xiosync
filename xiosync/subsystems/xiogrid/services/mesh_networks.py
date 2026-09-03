"""Mesh Network Service (abstracting mesh network integration)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.browser import (
    MeshNetwork,
    MeshNode,
)
from xiosync.platform.ids import new_id
from xiosync.services.events import EventService
from xiosync.services.operations import OperationService

__all__ = [
    "MeshNetworkRecord",
    "MeshNetworkService",
]


@dataclass(frozen=True, slots=True)
class MeshNetworkRecord:
    id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    network_type: str
    config: dict[str, Any]
    created_at: datetime


class MeshNetworkService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._event_service = EventService(session)

    def _record_op(
        self,
        ctx: OrgContext,
        operation: str,
        actor_id: uuid.UUID,
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
                depth_level=0,
            )
        )
        return op_id

    def create_network(
        self,
        ctx: OrgContext,
        *,
        project_id: uuid.UUID | None = None,
        name: str,
        network_type: str,
        config: Mapping[str, Any],
    ) -> MeshNetworkRecord:
        network_id = new_id()
        now = datetime.now(tz=UTC)
        
        row = MeshNetwork(
            id=network_id,
            organization_id=ctx.organization_id,
            project_id=project_id,
            name=name,
            network_type=network_type,
            config=dict(config),
            created_at=now,
        )
        self._session.add(row)
        
        op_id = self._record_op(ctx, "create_network", actor_id=ctx.actor_id or ctx.organization_id)
        self._event_service.append(
            ctx,
            event_type="mesh_network.created",
            payload={"network_id": str(network_id), "network_type": network_type},
            actor_id=ctx.actor_id,
            operation_id=op_id,
            entity_type="mesh_network",
            entity_id=network_id,
        )
        
        self._session.flush()
        return MeshNetworkRecord(
            id=row.id,
            organization_id=row.organization_id,
            project_id=row.project_id,
            name=row.name,
            network_type=row.network_type,
            config=row.config,
            created_at=row.created_at,
        )

    def add_node(
        self,
        ctx: OrgContext,
        *,
        network_id: uuid.UUID,
        node_id: uuid.UUID,
        address: str,
    ) -> None:
        now = datetime.now(tz=UTC)
        network = self._session.scalar(
            select(MeshNetwork).where(
                MeshNetwork.id == network_id,
                MeshNetwork.organization_id == ctx.organization_id
            )
        )
        if not network: raise ValueError(f"Network {network_id} not found")

        row = MeshNode(
            id=new_id(),
            organization_id=ctx.organization_id,
            project_id=network.project_id,
            network_id=network_id,
            node_id=node_id,
            address=address,
            created_at=now,
        )
        self._session.add(row)
        
        op_id = self._record_op(ctx, "add_node", actor_id=ctx.actor_id or ctx.organization_id)
        self._event_service.append(
            ctx,
            event_type="mesh_network.node_added",
            payload={"network_id": str(network_id), "node_id": str(node_id), "address": address},
            actor_id=ctx.actor_id,
            operation_id=op_id,
            entity_type="mesh_node",
            entity_id=row.id,
        )
        self._session.flush()

    def remove_node(
        self,
        ctx: OrgContext,
        *,
        network_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        row = self._session.scalar(
            select(MeshNode).where(
                MeshNode.organization_id == ctx.organization_id,
                MeshNode.network_id == network_id,
                MeshNode.node_id == node_id,
            )
        )
        if not row:
            raise ValueError(f"Node {node_id} not found in network {network_id}")
            
        self._session.delete(row)
        
        op_id = self._record_op(ctx, "remove_node", actor_id=ctx.actor_id or ctx.organization_id)
        self._event_service.append(
            ctx,
            event_type="mesh_network.node_removed",
            payload={"network_id": str(network_id), "node_id": str(node_id)},
            actor_id=ctx.actor_id,
            operation_id=op_id,
            entity_type="mesh_node",
            entity_id=row.id,
        )
        self._session.flush()

    def list_networks(self, ctx: OrgContext, *, project_id: uuid.UUID | None = None) -> list[MeshNetworkRecord]:
        stmt = select(MeshNetwork).where(MeshNetwork.organization_id == ctx.organization_id)
        if project_id is not None:
            stmt = stmt.where(MeshNetwork.project_id == project_id)
        rows = self._session.scalars(stmt).all()
        return [
            MeshNetworkRecord(
                id=r.id,
                organization_id=r.organization_id,
                project_id=r.project_id,
                name=r.name,
                network_type=r.network_type,
                config=r.config,
                created_at=r.created_at,
            )
            for r in rows
        ]
