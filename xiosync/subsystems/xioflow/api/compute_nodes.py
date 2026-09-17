"""compute_nodes.py — DB-backed compute node registry API.

Compute nodes are named, versioned code blobs that can be invoked as DAG
nodes of type ``compute_node``.  They are persisted in the
``compute_node_definitions`` table (see migration 0047).

Endpoints
---------
GET  /xioflow/compute-nodes/        — list all accessible nodes for this org
POST /xioflow/compute-nodes/        — register (upsert) a compute node
GET  /xioflow/compute-nodes/{name}  — fetch a single node by name
POST /xioflow/compute-nodes/{name}/enable   — enable for this org
POST /xioflow/compute-nodes/{name}/disable  — disable for this org
DELETE /xioflow/compute-nodes/{name}        — deregister (org-owned only)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.api.middleware.db import get_db
from xiosync.api.middleware.rbac import get_org_context
from xiosync.domain.context import OrgContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow/compute-nodes", tags=["XIOFLOW Compute Nodes"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class RegisterNodeRequest(BaseModel):
    name:         str
    description:  str = ""
    display_name: str = ""
    runtime:      str
    source_code:  str
    manifest:     dict[str, Any] = {}

    model_config = ConfigDict(from_attributes=True)


class ComputeNodeResponse(BaseModel):
    id:           str
    name:         str
    display_name: str
    description:  str
    runtime:      str
    source_code:  str
    manifest:     dict[str, Any]
    enabled:      bool
    created_at:   datetime
    updated_at:   datetime
    is_platform:  bool  # True when organization_id IS NULL

    model_config = ConfigDict(from_attributes=True)


def _row_to_response(row: Any) -> dict:
    return {
        "id":           str(row["id"]),
        "name":         row["name"],
        "display_name": row["display_name"] or "",
        "description":  row["description"] or "",
        "runtime":      row["runtime"],
        "source_code":  row["source_code"],
        "manifest":     row["manifest"] or {},
        "enabled":      bool(row["enabled"]),
        "created_at":   row["created_at"],
        "updated_at":   row["updated_at"],
        "is_platform":  row["organization_id"] is None,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[ComputeNodeResponse])
def list_compute_nodes(
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> list[dict]:
    """List all compute nodes accessible to this org (org-private + platform-global)."""
    rows = db.execute(
        text("""
            SELECT id, organization_id, name, display_name, description,
                   runtime, source_code, manifest, enabled, created_at, updated_at
            FROM   compute_node_definitions
            WHERE  organization_id = :org
               OR  organization_id IS NULL
            ORDER  BY organization_id NULLS LAST, name ASC
        """),
        {"org": str(ctx.organization_id)},
    ).mappings().all()
    return [_row_to_response(r) for r in rows]


@router.post("/", response_model=ComputeNodeResponse, status_code=201)
def register_compute_node(
    req: RegisterNodeRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Register or update a compute node definition (upsert by org + name)."""
    import json
    row = db.execute(
        text("""
            INSERT INTO compute_node_definitions
              (id, organization_id, name, display_name, description,
               runtime, source_code, manifest, enabled, created_at, updated_at)
            VALUES
              (gen_random_uuid(), :org, :name, :dname, :desc,
               :runtime, :src, cast(:manifest as jsonb), true, now(), now())
            ON CONFLICT (organization_id, name) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                description  = EXCLUDED.description,
                runtime      = EXCLUDED.runtime,
                source_code  = EXCLUDED.source_code,
                manifest     = EXCLUDED.manifest,
                updated_at   = now()
            RETURNING id, organization_id, name, display_name, description,
                      runtime, source_code, manifest, enabled, created_at, updated_at
        """),
        {
            "org":      str(ctx.organization_id),
            "name":     req.name,
            "dname":    req.display_name or req.name,
            "desc":     req.description,
            "runtime":  req.runtime,
            "src":      req.source_code,
            "manifest": json.dumps(req.manifest),
        },
    ).mappings().fetchone()
    db.commit()
    logger.info("compute_node.registered", extra={"name": req.name, "org": str(ctx.organization_id)})
    return _row_to_response(row)


@router.get("/{name}", response_model=ComputeNodeResponse)
def get_compute_node(
    name: str,
    db:   Session    = Depends(get_db),
    ctx:  OrgContext = Depends(get_org_context),
) -> dict:
    """Fetch a single compute node by name (org-private or platform-global)."""
    row = db.execute(
        text("""
            SELECT id, organization_id, name, display_name, description,
                   runtime, source_code, manifest, enabled, created_at, updated_at
            FROM   compute_node_definitions
            WHERE  name = :name
              AND  (organization_id = :org OR organization_id IS NULL)
            ORDER  BY organization_id NULLS LAST
            LIMIT  1
        """),
        {"name": name, "org": str(ctx.organization_id)},
    ).mappings().fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Compute node '{name}' not found")
    return _row_to_response(row)


@router.post("/{name}/enable", response_model=ComputeNodeResponse)
def enable_compute_node(
    name: str,
    db:   Session    = Depends(get_db),
    ctx:  OrgContext = Depends(get_org_context),
) -> dict:
    """Enable a compute node for this org."""
    row = db.execute(
        text("""
            UPDATE compute_node_definitions
            SET    enabled = true, updated_at = now()
            WHERE  name = :name AND organization_id = :org
            RETURNING id, organization_id, name, display_name, description,
                      runtime, source_code, manifest, enabled, created_at, updated_at
        """),
        {"name": name, "org": str(ctx.organization_id)},
    ).mappings().fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Compute node '{name}' not found for this org")
    db.commit()
    return _row_to_response(row)


@router.post("/{name}/disable", response_model=ComputeNodeResponse)
def disable_compute_node(
    name: str,
    db:   Session    = Depends(get_db),
    ctx:  OrgContext = Depends(get_org_context),
) -> dict:
    """Disable a compute node for this org."""
    row = db.execute(
        text("""
            UPDATE compute_node_definitions
            SET    enabled = false, updated_at = now()
            WHERE  name = :name AND organization_id = :org
            RETURNING id, organization_id, name, display_name, description,
                      runtime, source_code, manifest, enabled, created_at, updated_at
        """),
        {"name": name, "org": str(ctx.organization_id)},
    ).mappings().fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Compute node '{name}' not found for this org")
    db.commit()
    return _row_to_response(row)


@router.delete("/{name}", status_code=204)
def deregister_compute_node(
    name: str,
    db:   Session    = Depends(get_db),
    ctx:  OrgContext = Depends(get_org_context),
) -> None:
    """Delete an org-owned compute node definition (platform nodes cannot be deleted)."""
    result = db.execute(
        text("""
            DELETE FROM compute_node_definitions
            WHERE name = :name AND organization_id = :org
        """),
        {"name": name, "org": str(ctx.organization_id)},
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Compute node '{name}' not found for this org (platform nodes cannot be deleted via API)",
        )
    db.commit()
    logger.info("compute_node.deregistered", extra={"name": name, "org": str(ctx.organization_id)})


# ── Router registration ────────────────────────────────────────────────────────
from xiosync.api.router_registry import register_router  # noqa: E402
from xiosync.api.middleware.rbac import require_capability  # noqa: E402

register_router(
    router,
    prefix="/api/v1",
    tags=["XIOFLOW Compute Nodes"],
    dependencies=[require_capability("workflow.manage")],
)
