"""XIOFlow Agent API — CRUD + execution endpoints for AI Agent actors.

Routes:
  POST   /agents                     Create a new agent
  GET    /agents                     List agents for the current org
  GET    /agents/{agent_id}          Get agent details
  PATCH  /agents/{agent_id}          Update agent configuration
  DELETE /agents/{agent_id}          Soft-delete an agent
  POST   /agents/{agent_id}/run      Execute a goal through the agent
  GET    /agents/{agent_id}/runs     List agent execution history
  GET    /agents/runs/{run_id}       Get a specific agent run
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow/agents", tags=["xioflow-agents"])


# ── Pydantic Models ──────────────────────────────────────────────────────


class CreateAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(None, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str | None = None
    agent_type: str = Field(
        "browser_automation",
        pattern=r"^(browser_automation|data_extraction|system_admin|orchestrator|custom)$",
    )
    project_id: str | None = None
    model_config_json: dict[str, Any] = Field(default_factory=dict, alias="model_config")
    tool_bindings: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str | None = None
    memory_scope: str = Field("project", pattern=r"^(project|organization|global)$")
    max_concurrent_runs: int = Field(1, ge=1, le=100)

    model_config = {"populate_by_name": True}


class UpdateAgentRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    model_config_json: dict[str, Any] | None = Field(None, alias="model_config")
    tool_bindings: dict[str, Any] | None = None
    system_prompt: str | None = None
    memory_scope: str | None = Field(None, pattern=r"^(project|organization|global)$")
    max_concurrent_runs: int | None = Field(None, ge=1, le=100)
    is_enabled: bool | None = None

    model_config = {"populate_by_name": True}


class RunAgentRequest(BaseModel):
    goal: str = Field(..., min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(0, ge=0, le=100)


# ── Dependency Injection ─────────────────────────────────────────────────


def _get_db():
    """Get a SQLAlchemy session from the platform engine."""
    from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415

    session = Session(get_engine())
    try:
        yield session
    finally:
        session.close()


def _get_org_id(request) -> str:
    """Extract organization_id from the authenticated request."""
    org_id = getattr(request.state, "organization_id", None)
    if not org_id:
        raise HTTPException(status_code=403, detail="No organization context")
    return str(org_id)


def _get_actor_id(request) -> str:
    """Extract actor_id from the authenticated request."""
    actor_id = getattr(request.state, "actor_id", None)
    if not actor_id:
        raise HTTPException(status_code=403, detail="No actor context")
    return str(actor_id)


# ── Routes ───────────────────────────────────────────────────────────────


@router.post("", status_code=201)
async def create_agent(
    body: CreateAgentRequest,
    request=None,
    db: Session = Depends(_get_db),
):
    """Create a new agent with an associated actor identity."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
        creator_actor_id = _get_actor_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    # Auto-generate slug from name if not provided
    slug = body.slug or re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")

    # Check uniqueness
    existing = db.execute(
        text(
            "SELECT id FROM xioflow_agents "
            "WHERE organization_id = :org_id AND slug = :slug AND deleted_at IS NULL"
        ),
        {"org_id": org_id, "slug": slug},
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Agent slug '{slug}' already exists")

    # Create an actor identity for this agent (actor_type='agent')
    agent_actor_id = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO actors "
            "(id, organization_id, actor_type, actor_subtype, alias, "
            " state, lifecycle_phase, trust_tier, health_status, created_by, created_at) "
            "VALUES (:id, :org_id, 'agent', :subtype, :alias, "
            " 'active', 'operational', 'contributor', 'healthy', :created_by, now())"
        ),
        {
            "id": agent_actor_id,
            "org_id": org_id,
            "subtype": body.agent_type,
            "alias": body.name,
            "created_by": creator_actor_id,
        },
    )

    # Create the agent
    agent_id = str(uuid.uuid4())
    import json  # noqa: PLC0415

    db.execute(
        text(
            "INSERT INTO xioflow_agents "
            "(id, organization_id, project_id, name, slug, description, "
            " agent_type, actor_id, model_config, tool_bindings, system_prompt, "
            " memory_scope, max_concurrent_runs, created_at) "
            "VALUES (:id, :org_id, :project_id, :name, :slug, :description, "
            " :agent_type, :actor_id, :model_config::jsonb, :tool_bindings::jsonb, "
            " :system_prompt, :memory_scope, :max_concurrent, now())"
        ),
        {
            "id": agent_id,
            "org_id": org_id,
            "project_id": body.project_id,
            "name": body.name,
            "slug": slug,
            "description": body.description,
            "agent_type": body.agent_type,
            "actor_id": agent_actor_id,
            "model_config": json.dumps(body.model_config_json),
            "tool_bindings": json.dumps(body.tool_bindings),
            "system_prompt": body.system_prompt,
            "memory_scope": body.memory_scope,
            "max_concurrent": body.max_concurrent_runs,
        },
    )
    db.commit()

    logger.info(f"agent.created: id={agent_id} slug={slug} type={body.agent_type}")

    return {
        "id": agent_id,
        "slug": slug,
        "actor_id": agent_actor_id,
        "agent_type": body.agent_type,
        "state": "idle",
    }


@router.get("")
async def list_agents(
    request=None,
    agent_type: str | None = Query(None),
    state: str | None = Query(None),
    db: Session = Depends(_get_db),
):
    """List agents for the current organization."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    where = "organization_id = :org_id AND deleted_at IS NULL"
    params: dict[str, Any] = {"org_id": org_id}

    if agent_type:
        where += " AND agent_type = :agent_type"
        params["agent_type"] = agent_type
    if state:
        where += " AND state = :state"
        params["state"] = state

    rows = db.execute(
        text(f"SELECT * FROM xioflow_agents WHERE {where} ORDER BY created_at DESC"),
        params,
    ).mappings().all()

    return {"agents": [dict(r) for r in rows], "total": len(rows)}


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str,
    request=None,
    db: Session = Depends(_get_db),
):
    """Get agent details."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    row = db.execute(
        text(
            "SELECT * FROM xioflow_agents "
            "WHERE id = :id AND organization_id = :org_id AND deleted_at IS NULL"
        ),
        {"id": agent_id, "org_id": org_id},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")

    return dict(row)


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    body: UpdateAgentRequest,
    request=None,
    db: Session = Depends(_get_db),
):
    """Update agent configuration."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    updates = body.model_dump(exclude_none=True, by_alias=False)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses = []
    params: dict[str, Any] = {"id": agent_id, "org_id": org_id}

    import json  # noqa: PLC0415

    for key, value in updates.items():
        col = "model_config" if key == "model_config_json" else key
        if isinstance(value, dict):
            set_clauses.append(f"{col} = :{key}::jsonb")
            params[key] = json.dumps(value)
        else:
            set_clauses.append(f"{col} = :{key}")
            params[key] = value

    set_clauses.append("updated_at = now()")

    result = db.execute(
        text(
            f"UPDATE xioflow_agents SET {', '.join(set_clauses)} "
            f"WHERE id = :id AND organization_id = :org_id AND deleted_at IS NULL"
        ),
        params,
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {"updated": True, "agent_id": agent_id}


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: str,
    request=None,
    db: Session = Depends(_get_db),
):
    """Soft-delete an agent."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    result = db.execute(
        text(
            "UPDATE xioflow_agents SET deleted_at = now(), state = 'archived', updated_at = now() "
            "WHERE id = :id AND organization_id = :org_id AND deleted_at IS NULL"
        ),
        {"id": agent_id, "org_id": org_id},
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {"deleted": True, "agent_id": agent_id}


@router.post("/{agent_id}/run")
async def run_agent(
    agent_id: str,
    body: RunAgentRequest,
    request=None,
    db: Session = Depends(_get_db),
):
    """Execute a semantic goal through an agent's cognitive loop."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    # Verify agent exists and belongs to this org
    agent = db.execute(
        text(
            "SELECT id, state, is_enabled FROM xioflow_agents "
            "WHERE id = :id AND organization_id = :org_id AND deleted_at IS NULL"
        ),
        {"id": agent_id, "org_id": org_id},
    ).mappings().first()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent["is_enabled"]:
        raise HTTPException(status_code=409, detail="Agent is disabled")
    if agent["state"] not in ("idle", "errored"):
        raise HTTPException(
            status_code=409,
            detail=f"Agent is busy (state={agent['state']})",
        )

    from xiosync.subsystems.xioflow.engine.agent_executor import (  # noqa: PLC0415
        AgentExecutor,
    )

    executor = AgentExecutor(session=db)
    result = await executor.execute_goal(
        agent_id=agent_id,
        goal=body.goal,
        context={**body.context, "priority": body.priority},
    )

    status_code = 200 if result.get("success") else 500
    return result


@router.get("/{agent_id}/runs")
async def list_agent_runs(
    agent_id: str,
    request=None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(_get_db),
):
    """List execution history for a specific agent."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    rows = db.execute(
        text(
            "SELECT * FROM xioflow_agent_runs "
            "WHERE agent_id = :agent_id AND organization_id = :org_id "
            "ORDER BY started_at DESC LIMIT :limit OFFSET :offset"
        ),
        {"agent_id": agent_id, "org_id": org_id, "limit": limit, "offset": offset},
    ).mappings().all()

    return {"runs": [dict(r) for r in rows], "total": len(rows)}


@router.get("/runs/{run_id}")
async def get_agent_run(
    run_id: str,
    request=None,
    db: Session = Depends(_get_db),
):
    """Get a specific agent run by ID."""
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    row = db.execute(
        text(
            "SELECT * FROM xioflow_agent_runs "
            "WHERE id = :id AND organization_id = :org_id"
        ),
        {"id": run_id, "org_id": org_id},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Agent run not found")

    return dict(row)


# ── Multi-Agent Coordination ─────────────────────────────────────────────


class CoordinateRequest(BaseModel):
    goal: str = Field(..., min_length=1)
    tasks: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        description="List of tasks with description, agent_type, depends_on, etc.",
    )
    context: dict[str, Any] = Field(default_factory=dict)


@router.post("/coordinate")
async def coordinate_agents(
    body: CoordinateRequest,
    request=None,
    db: Session = Depends(_get_db),
):
    """Execute a multi-agent coordination plan.

    Decomposes a cross-domain goal into parallelizable sub-tasks,
    auto-assigns agents by capability, manages data dependencies,
    and aggregates results.

    Example body:
    ```json
    {
      "goal": "Sign into Google, extract billing, post to Slack",
      "tasks": [
        {"description": "Sign into Google accounts", "agent_type": "browser_automation", "domain": "accounts.google.com"},
        {"description": "Extract billing data", "agent_type": "data_extraction", "depends_on": ["task_0"]},
        {"description": "Post summary to Slack", "agent_type": "system_admin", "depends_on": ["task_1"]}
      ]
    }
    ```
    """
    from starlette.requests import Request  # noqa: PLC0415

    if isinstance(request, Request):
        org_id = _get_org_id(request)
    else:
        raise HTTPException(status_code=403, detail="Request context required")

    from xiosync.subsystems.xioflow.engine.coordinator import (  # noqa: PLC0415
        MultiAgentCoordinator,
    )

    coordinator = MultiAgentCoordinator(session=db)
    result = await coordinator.coordinate(
        org_id=org_id,
        goal=body.goal,
        tasks=body.tasks,
        context=body.context,
    )

    return result

