"""XIOFLOW Memory API — Teacher Extension action recording + graph inspection."""
from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext
from xiosync.platform.ids import new_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow/memory", tags=["XIOFLOW Memory"])


def _session(request: Request) -> OrmSession:
    return cast(OrmSession, request.state.org_session)


def _ctx(request: Request) -> OrgContext:
    return cast(OrgContext, request.state.org_context)


# ── Request models ────────────────────────────────────────────────────────────

class RecordRequest(BaseModel):
    url: str
    intent: str
    face_value: dict[str, Any]
    place_value: dict[str, Any]
    action_type: str
    action_params: dict[str, Any]
    previous_node_id: str | None = None
    project_id: str | None = None
    context_hash: str | None = None
    # tier: project_experimental | project_ground_truth | organization_shared | platform_global
    tier: str = "project_experimental"
    execution_mode: str = "sequential"
    model_config = ConfigDict(from_attributes=True)


class VoteRequest(BaseModel):
    node_id: str
    raw_vote: float
    tier_confidence: float
    winning_tier: int | None = None
    context_hash: str | None = None
    model_config = ConfigDict(from_attributes=True)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/record", summary="Record a Teacher Extension action as a memory node")
def record_action(req: RecordRequest, request: Request) -> dict[str, Any]:
    """Inserts a new XioflowMemoryNode row.

    Called by any recording extension or worker after each user action.
    The node is linked to its predecessor via action_params.previous_node_id.
    """
    session = _session(request)
    ctx = _ctx(request)

    from urllib.parse import urlparse
    domain = urlparse(req.url).netloc or req.url

    node_id = new_id()
    session.execute(
        text("""
            INSERT INTO xioflow_memory_nodes
              (id, organization_id, project_id, tier, status, domain, intent,
               context_hash, face_value, place_value, action_type, action_params,
               execution_mode, previous_intent, created_at)
            VALUES
              (:id, :org, :proj, :tier, 'ACTIVE', :domain, :intent,
               :ctx_hash, cast(:face as jsonb), cast(:place as jsonb),
               :action_type, cast(:action_params as jsonb),
               :exec_mode, :prev_intent, now())
        -- tier: project_experimental | project_ground_truth | organization_shared | platform_global
        -- status: ACTIVE | ARCHIVED | DEPRECATED
        """),
        {
            "id": str(node_id),
            "org": str(ctx.organization_id),
            "proj": req.project_id,
            "tier": req.tier,
            "domain": domain,
            "intent": req.intent,
            "ctx_hash": req.context_hash or "",
            "face": __import__("json").dumps(req.face_value),
            "place": __import__("json").dumps(req.place_value),
            "action_type": req.action_type,
            "action_params": __import__("json").dumps(req.action_params),
            "exec_mode": req.execution_mode,
            "prev_intent": None,
        },
    )

    # Link previous node → this node (append to next_nodes JSONB array)
    if req.previous_node_id:
        session.execute(
            text("""
                UPDATE xioflow_memory_nodes
                SET next_nodes = coalesce(next_nodes, '[]'::jsonb)
                                 || cast(:new_id as jsonb)
                WHERE id = cast(:prev_id as uuid)
                  AND organization_id = :org
            """),
            {
                "new_id": __import__("json").dumps(str(node_id)),
                "prev_id": req.previous_node_id,
                "org": str(ctx.organization_id),
            },
        )

    session.commit()
    logger.info("memory.record", extra={"node_id": str(node_id), "intent": req.intent})
    return {"status": "success", "node_id": str(node_id)}


@router.post("/vote", summary="Submit a consensus vote on a memory node")
def submit_vote(req: VoteRequest, request: Request) -> dict[str, Any]:
    """Adjust a node's tier confidence score (consensus learning)."""
    session = _session(request)
    ctx = _ctx(request)

    # Clamp raw_vote to [-1, 1]
    score = max(-1.0, min(1.0, req.raw_vote))

    result = session.execute(
        text("""
            UPDATE xioflow_memory_nodes
            SET    tier = CASE
                            WHEN :score > 0.6  THEN 'platform_global'
                            WHEN :score > 0.2  THEN 'organization_shared'
                            WHEN :score > -0.2 THEN 'project_ground_truth'
                            ELSE 'project_experimental'
                          END
            WHERE  id = cast(:node_id as uuid)
              AND  organization_id = :org
            RETURNING id
        """),
        {"score": score, "node_id": req.node_id, "org": str(ctx.organization_id)},
    ).fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="node_not_found")

    return {"status": "success", "node_id": req.node_id, "score_applied": score}


@router.post("/promote/{node_id}", summary="Manually promote a node to gold tier")
def promote_node(node_id: str, request: Request) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)
    session.execute(
        text("UPDATE xioflow_memory_nodes SET tier='platform_global' WHERE id=cast(:id as uuid) AND organization_id=:org"),
        {"id": node_id, "org": str(ctx.organization_id)},
    )
    session.commit()
    return {"status": "success", "node_id": node_id, "tier": "platform_global"}


@router.post("/demote/{node_id}", summary="Manually demote a node to deprecated")
def demote_node(node_id: str, request: Request) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)
    session.execute(
        text("UPDATE xioflow_memory_nodes SET tier='project_experimental' WHERE id=cast(:id as uuid) AND organization_id=:org"),
        {"id": node_id, "org": str(ctx.organization_id)},
    )
    session.commit()
    return {"status": "success", "node_id": node_id, "tier": "project_experimental"}


@router.get("/search", summary="Search memory nodes by intent keyword")
def search_intents(request: Request, q: str, limit: int = 10) -> list[dict[str, Any]]:
    session = _session(request)
    ctx = _ctx(request)
    rows = session.execute(
        text("""
            SELECT id, domain, intent, action_type, tier, status
            FROM   xioflow_memory_nodes
            WHERE  organization_id = :org
              AND  (intent ILIKE :q OR domain ILIKE :q)
            ORDER BY tier DESC, id
            LIMIT :lim
        """),
        {"org": str(ctx.organization_id), "q": f"%{q}%", "lim": limit},
    ).fetchall()
    return [
        {"id": str(r.id), "domain": r.domain, "intent": r.intent,
         "action_type": r.action_type, "tier": r.tier, "status": r.status}
        for r in rows
    ]


@router.get("/graph", summary="Get full DAG for a domain+intent")
def get_graph(request: Request, domain: str, intent: str) -> dict[str, Any]:
    session = _session(request)
    ctx = _ctx(request)
    from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph
    graph = MemoryGraph(session, org_id=ctx.organization_id)
    try:
        return graph.get_workflow_graph(domain, intent)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
