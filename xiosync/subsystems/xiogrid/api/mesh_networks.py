"""Mesh Networks & Nodes — full CRUD management API.

Networks are logical groupings of mesh nodes (Tailscale, WireGuard, etc.).
Multiple networks can be registered per org. Nodes are registered under a
specific network and can carry a stable MESH-{serial} binding (linked via
mesh_node_bindings) for Colab workers.

Endpoints:
  POST   /mesh-networks                       — create network
  GET    /mesh-networks                       — list networks
  GET    /mesh-networks/{id}                  — get network + node list
  PATCH  /mesh-networks/{id}                  — update name/config/state
  DELETE /mesh-networks/{id}                  — delete network (cascades nodes)
  POST   /mesh-networks/{id}/nodes            — register node
  GET    /mesh-networks/{id}/nodes            — list nodes + binding info
  GET    /mesh-networks/{id}/nodes/{node_id}  — get node detail + binding
  DELETE /mesh-networks/{id}/nodes/{node_id}  — remove node
"""
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["mesh-networks"])


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Request Bodies ─────────────────────────────────────────────────────────────

class CreateNetworkRequest(_S):
    name: str
    network_type: str                    # tailscale | wireguard | zerotier | overlay
    config: dict[str, Any] = {}
    project_id: uuid.UUID | None = None


class UpdateNetworkRequest(_S):
    name: str | None = None
    config: dict[str, Any] | None = None
    state: str | None = None             # active | configuring | error | disabled


class AddNodeRequest(_S):
    node_id: uuid.UUID                   # actor / worker UUID
    node_address: str                    # Tailscale IP or hostname
    runtime_type: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _problem(status: int, code: str, title: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {
        "type":   f"https://xiosync.dev/problems/{code}",
        "title":  title,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status, media_type="application/problem+json", content=body)


# ── Network CRUD ───────────────────────────────────────────────────────────────

@router.post("/mesh-networks", status_code=201, summary="Create a mesh network", response_model=None)
def create_network(payload: CreateNetworkRequest, request: Request) -> dict[str, Any] | JSONResponse:
    """Create a new mesh network under the authenticated org."""
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.services.mesh_networks import MeshNetworkService

    ctx     = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc     = MeshNetworkService(session)
    try:
        net = svc.create_network(
            ctx,
            name=payload.name,
            network_type=payload.network_type,
            config=payload.config,
            project_id=payload.project_id,
        )
        session.flush()  # middleware auto-commits on exit
        return {
            "id":           str(net.id),
            "name":         net.name,
            "network_type": net.network_type,
            "config":       net.config,
            "state":        "active",
            "created_at":   net.created_at.isoformat(),
        }
    except Exception as exc:

        return _problem(422, "network_creation_failed", "Network creation failed", str(exc))


@router.get("/mesh-networks", summary="List mesh networks", response_model=None)
def list_networks(
    request: Request,
    project_id: uuid.UUID | None = Query(default=None, description="Filter by project"),
) -> dict[str, Any] | JSONResponse:
    """List all mesh networks for the authenticated org, with node counts."""
    from sqlalchemy import select, func
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.models.browser import MeshNetwork, MeshNode

    ctx     = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    stmt = select(MeshNetwork).where(MeshNetwork.organization_id == ctx.organization_id)
    if project_id is not None:
        stmt = stmt.where(MeshNetwork.project_id == project_id)
    rows = session.scalars(stmt).all()

    node_counts: dict[uuid.UUID, int] = {}
    if rows:
        counts = session.execute(
            select(MeshNode.network_id, func.count(MeshNode.id).label("cnt"))
            .where(MeshNode.network_id.in_([r.id for r in rows]))
            .group_by(MeshNode.network_id)
        ).all()
        node_counts = {row.network_id: row.cnt for row in counts}

    return {
        "networks": [
            {
                "id":           str(r.id),
                "name":         r.name,
                "network_type": r.network_type,
                "state":        r.state,
                "node_count":   node_counts.get(r.id, 0),
                "created_at":   r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/mesh-networks/{network_id}", summary="Get mesh network detail", response_model=None)
def get_network(network_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    """Get a single mesh network with its full node list."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.models.browser import MeshNetwork, MeshNode

    ctx     = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    net = session.scalar(
        select(MeshNetwork).where(
            MeshNetwork.id == network_id,
            MeshNetwork.organization_id == ctx.organization_id,
        )
    )
    if not net:
        return _problem(404, "not_found", "Mesh network not found")

    nodes = session.scalars(
        select(MeshNode).where(
            MeshNode.network_id == network_id,
            MeshNode.organization_id == ctx.organization_id,
        )
    ).all()

    return {
        "id":           str(net.id),
        "name":         net.name,
        "network_type": net.network_type,
        "config":       net.config,
        "state":        net.state,
        "created_at":   net.created_at.isoformat(),
        "updated_at":   net.updated_at.isoformat() if net.updated_at else None,
        "node_count":   len(nodes),
        "nodes": [
            {
                "id":           str(n.id),
                "node_id":      str(n.node_id),
                "address":      n.address,
                "serial":       n.serial,
                "mesh_name":    f"MESH-{n.serial:03d}" if n.serial else None,
                "runtime_type": n.runtime_type,
                "created_at":   n.created_at.isoformat(),
            }
            for n in nodes
        ],
    }


@router.patch("/mesh-networks/{network_id}", summary="Update mesh network", response_model=None)
def update_network(
    network_id: uuid.UUID,
    payload: UpdateNetworkRequest,
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """Update a mesh network name, config, or lifecycle state."""
    from datetime import datetime, UTC
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.models.browser import MeshNetwork

    VALID_STATES = {"active", "configuring", "error", "disabled"}
    if payload.state and payload.state not in VALID_STATES:
        return _problem(422, "invalid_state",
                        f"Invalid state — must be one of: {', '.join(sorted(VALID_STATES))}")

    ctx     = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    net = session.scalar(
        select(MeshNetwork).where(
            MeshNetwork.id == network_id,
            MeshNetwork.organization_id == ctx.organization_id,
        )
    )
    if not net:
        return _problem(404, "not_found", "Mesh network not found")

    if payload.name   is not None: net.name   = payload.name
    if payload.config is not None: net.config = payload.config
    if payload.state  is not None: net.state  = payload.state
    net.updated_at = datetime.now(UTC)
    session.flush()

    return {
        "id":           str(net.id),
        "name":         net.name,
        "network_type": net.network_type,
        "config":       net.config,
        "state":        net.state,
        "updated_at":   net.updated_at.isoformat(),
    }


@router.delete("/mesh-networks/{network_id}", summary="Delete mesh network", response_model=None)
def delete_network(network_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    """Delete a mesh network. Nodes cascade-delete via FK ON DELETE CASCADE."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.models.browser import MeshNetwork

    ctx     = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    net = session.scalar(
        select(MeshNetwork).where(
            MeshNetwork.id == network_id,
            MeshNetwork.organization_id == ctx.organization_id,
        )
    )
    if not net:
        return _problem(404, "not_found", "Mesh network not found")

    session.delete(net)
    return {"id": str(network_id), "deleted": True}


# ── Node CRUD ──────────────────────────────────────────────────────────────────

@router.post(
    "/mesh-networks/{network_id}/nodes",
    status_code=201,
    summary="Register a node under a network",
    response_model=None,
)
def add_node(
    network_id: uuid.UUID,
    payload: AddNodeRequest,
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """Register a new mesh node under the specified network."""
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.services.mesh_networks import MeshNetworkService

    ctx     = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc     = MeshNetworkService(session)
    try:
        svc.add_node(
            ctx,
            network_id=network_id,
            node_id=payload.node_id,
            address=payload.node_address,
            runtime_type=payload.runtime_type,
        )
        session.flush()  # middleware auto-commits on exit
        return {
            "network_id":  str(network_id),
            "node_id":     str(payload.node_id),
            "address":     payload.node_address,
            "runtime_type": payload.runtime_type,
        }
    except ValueError as exc:
        return _problem(404, "not_found", str(exc))
    except Exception as exc:

        return _problem(422, "add_node_failed", "Add node failed", str(exc))


@router.get(
    "/mesh-networks/{network_id}/nodes",
    summary="List nodes in a network",
    response_model=None,
)
def list_nodes(network_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    """List all nodes in a mesh network, with MESH binding info where available."""
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.models.browser import MeshNetwork, MeshNode

    ctx     = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    net = session.scalar(
        select(MeshNetwork).where(
            MeshNetwork.id == network_id,
            MeshNetwork.organization_id == ctx.organization_id,
        )
    )
    if not net:
        return _problem(404, "not_found", "Mesh network not found")

    nodes = session.scalars(
        select(MeshNode).where(
            MeshNode.network_id == network_id,
            MeshNode.organization_id == ctx.organization_id,
        )
    ).all()

    # Fetch bindings in bulk
    binding_map: dict[str, tuple] = {}
    if nodes:
        node_ids = [str(n.id) for n in nodes]
        rows = session.execute(text("""
            SELECT mesh_node_id::text, colab_account, last_used_at
            FROM mesh_node_bindings
            WHERE mesh_node_id IN (
                SELECT unnest(CAST(:ids AS uuid[]))
            )
        """), {"ids": node_ids}).all()
        binding_map = {r[0]: r for r in rows}

    return {
        "network_id":   str(network_id),
        "network_name": net.name,
        "nodes": [
            {
                "id":            str(n.id),
                "node_id":       str(n.node_id),
                "address":       n.address,
                "serial":        n.serial,
                "mesh_name":     f"MESH-{n.serial:03d}" if n.serial else None,
                "runtime_type":  n.runtime_type,
                "colab_account": binding_map.get(str(n.id), (None, None, None))[1],
                "last_used_at": (
                    binding_map[str(n.id)][2].isoformat()
                    if str(n.id) in binding_map and binding_map[str(n.id)][2] else None
                ),
                "created_at":    n.created_at.isoformat(),
            }
            for n in nodes
        ],
    }


@router.get(
    "/mesh-networks/{network_id}/nodes/{node_id}",
    summary="Get node detail",
    response_model=None,
)
def get_node(
    network_id: uuid.UUID,
    node_id: uuid.UUID,
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """Get a single node's detail including its MESH-serial binding if any."""
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.models.browser import MeshNode

    ctx     = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    node = session.scalar(
        select(MeshNode).where(
            MeshNode.id == node_id,
            MeshNode.network_id == network_id,
            MeshNode.organization_id == ctx.organization_id,
        )
    )
    if not node:
        return _problem(404, "not_found", "Node not found in this network")

    binding = session.execute(text("""
        SELECT colab_account, last_used_at
        FROM mesh_node_bindings WHERE mesh_node_id = :id
    """), {"id": str(node_id)}).one_or_none()

    return {
        "id":            str(node.id),
        "network_id":    str(node.network_id),
        "node_id":       str(node.node_id),
        "address":       node.address,
        "serial":        node.serial,
        "mesh_name":     f"MESH-{node.serial:03d}" if node.serial else None,
        "runtime_type":  node.runtime_type,
        "colab_account": binding[0] if binding else None,
        "last_used_at":  binding[1].isoformat() if binding and binding[1] else None,
        "created_at":    node.created_at.isoformat(),
    }


@router.delete(
    "/mesh-networks/{network_id}/nodes/{node_id}",
    summary="Remove node from network",
    response_model=None,
)
def remove_node(
    network_id: uuid.UUID,
    node_id: uuid.UUID,
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """Remove a node from a mesh network by its mesh_nodes.id (not node_id actor UUID)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.subsystems.xiogrid.models.browser import MeshNode

    ctx     = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    node = session.scalar(
        select(MeshNode).where(
            MeshNode.id == node_id,
            MeshNode.network_id == network_id,
            MeshNode.organization_id == ctx.organization_id,
        )
    )
    if not node:
        return _problem(404, "not_found", "Node not found in this network")

    session.delete(node)
    return {"id": str(node_id), "removed": True}
