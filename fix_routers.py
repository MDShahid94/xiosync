import uuid
import sys

def rewrite_mesh():
    content = """\"\"\"Mesh Networks CRUD API endpoints.\"\"\"
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["mesh-networks"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateNetworkRequest(_S):
    name: str
    provider: str
    config: dict[str, Any] = {}

class AddNodeRequest(_S):
    node_id: uuid.UUID
    node_address: str

@router.post("/mesh-networks", status_code=201, summary="Create network", response_model=None)
def create_network(payload: CreateNetworkRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.mesh_networks import MeshNetworkService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = MeshNetworkService(session)

    try:
        net = svc.create_network(ctx, name=payload.name, provider=payload.provider, config=payload.config)
        return {"id": str(net.id), "name": net.name}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/network_creation_failed",
                "title": "Network creation failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/mesh-networks", summary="List networks", response_model=None)
def list_networks(request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.mesh_networks import MeshNetworkService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = MeshNetworkService(session)

    networks = svc.list_networks(ctx)
    return {"networks": [{"id": str(n.id), "name": n.name} for n in networks]}

@router.post("/mesh-networks/{network_id}/nodes", status_code=201, summary="Add node", response_model=None)
def add_node(network_id: uuid.UUID, payload: AddNodeRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.mesh_networks import MeshNetworkService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = MeshNetworkService(session)

    try:
        svc.add_node(ctx, network_id=network_id, node_id=payload.node_id, address=payload.node_address)
        return {"network_id": str(network_id), "node_id": str(payload.node_id)}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/add_node_failed",
                "title": "Add node failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.delete("/mesh-networks/{network_id}/nodes/{node_id}", summary="Remove node", response_model=None)
def remove_node(network_id: uuid.UUID, node_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.mesh_networks import MeshNetworkService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = MeshNetworkService(session)

    try:
        svc.remove_node(ctx, network_id=network_id, node_id=node_id)
        return {"id": str(node_id), "removed": True}
    except Exception as exc:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Node not found or remove failed",
                "status": 404,
                "detail": str(exc),
            },
        )
"""
    with open("xiosync/api/routers/mesh_networks.py", "w") as f:
        f.write(content)

def rewrite_compute():
    content = """\"\"\"Compute Runtimes CRUD API endpoints.\"\"\"
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["compute-runtimes"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class RegisterProviderRequest(_S):
    name: str
    provider_type: str
    config: dict[str, Any] = {}

class ProvisionNodeRequest(_S):
    instance_type: str
    spec: dict[str, Any] = {}

@router.post("/compute-runtimes", status_code=201, summary="Register provider", response_model=None)
def register_provider(payload: RegisterProviderRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.compute_runtimes import ComputeRuntimeService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ComputeRuntimeService(session)

    try:
        provider = svc.register_provider(ctx, name=payload.name, provider_type=payload.provider_type, config=payload.config)
        return {"id": str(provider.id), "name": provider.name}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/provider_registration_failed",
                "title": "Provider registration failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/compute-runtimes", summary="List providers", response_model=None)
def list_providers(request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy import select
    from xiosync.domain.context import OrgContext
    from xiosync.persistence.models.compute import RuntimeProvider

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    rows = session.scalars(select(RuntimeProvider).where(RuntimeProvider.organization_id == ctx.organization_id)).all()
    return {"providers": [{"id": str(p.id), "name": p.name} for p in rows]}

@router.post("/compute-runtimes/{provider_id}/nodes", status_code=201, summary="Provision node", response_model=None)
def provision_node(provider_id: uuid.UUID, payload: ProvisionNodeRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.compute_runtimes import ComputeRuntimeService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ComputeRuntimeService(session)

    try:
        node = svc.provision_node(ctx, provider_id=provider_id, spec=payload.spec)
        return {"id": str(node.id), "provider_id": str(node.provider_id)}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/provision_failed",
                "title": "Provision failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/compute-runtimes/{provider_id}/nodes", summary="List nodes", response_model=None)
def list_nodes(provider_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.compute_runtimes import ComputeRuntimeService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ComputeRuntimeService(session)

    nodes = svc.list_nodes(ctx, provider_id=provider_id)
    return {"nodes": [{"id": str(n.id), "provider_id": str(n.provider_id)} for n in nodes]}

@router.delete("/compute-runtimes/nodes/{node_id}", summary="Terminate node", response_model=None)
def terminate_node(node_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.compute_runtimes import ComputeRuntimeService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = ComputeRuntimeService(session)

    try:
        svc.terminate_node(ctx, node_id)
        return {"id": str(node_id), "terminated": True}
    except Exception as exc:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Node not found or terminate failed",
                "status": 404,
                "detail": str(exc),
            },
        )
"""
    with open("xiosync/api/routers/compute_runtimes.py", "w") as f:
        f.write(content)

def rewrite_browser_sessions():
    content = """\"\"\"Browser Sessions CRUD API endpoints.\"\"\"
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["browser-sessions"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateBrowserSessionRequest(_S):
    pool_id: uuid.UUID

@router.post("/browser-sessions", status_code=201, summary="Create a browser session", response_model=None)
def create_session(payload: CreateBrowserSessionRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_sessions import BrowserSessionService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserSessionService(session)

    try:
        sess = svc.create_session(ctx, pool_id=payload.pool_id)
        return {"id": str(sess.id), "pool_id": str(sess.pool_id)}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/session_creation_failed",
                "title": "Browser session creation failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/browser-sessions", summary="List browser sessions", response_model=None)
def list_sessions(request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_sessions import BrowserSessionService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserSessionService(session)

    sessions = svc.list_sessions(ctx)
    return {"sessions": [{"id": str(s.id), "pool_id": str(s.pool_id)} for s in sessions]}

@router.get("/browser-sessions/{session_id}/health", summary="Get browser session health", response_model=None)
def get_session_health(session_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_sessions import BrowserSessionService, BrowserSessionNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserSessionService(session)

    try:
        health = svc.verify_session(ctx, session_id)
        # SessionHealthRecord probably has some fields we can return
        return {"id": str(session_id), "status": health.status if hasattr(health, 'status') else "ok"}
    except BrowserSessionNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser session not found",
                "status": 404,
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/health_check_failed",
                "title": "Health check failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.delete("/browser-sessions/{session_id}", summary="Terminate browser session", response_model=None)
def terminate_session(session_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_sessions import BrowserSessionService, BrowserSessionNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserSessionService(session)

    try:
        svc.terminate_session(ctx, session_id)
        return {"id": str(session_id), "terminated": True}
    except BrowserSessionNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser session not found",
                "status": 404,
            },
        )
"""
    with open("xiosync/api/routers/browser_sessions.py", "w") as f:
        f.write(content)

def rewrite_browser_pools():
    content = """\"\"\"Browser Pools CRUD API endpoints.\"\"\"
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["browser-pools"])

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class CreateBrowserPoolRequest(_S):
    name: str
    engine_type: str = "chromium"
    max_instances: int = 5

class ScaleBrowserPoolRequest(_S):
    replicas: int

@router.post("/browser-pools", status_code=201, summary="Create a browser pool", response_model=None)
def create_pool(payload: CreateBrowserPoolRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    try:
        pool = svc.create_pool(ctx, name=payload.name, engine_type=payload.engine_type, max_instances=payload.max_instances)
        return {"id": str(pool.id), "name": pool.name}
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/pool_creation_failed",
                "title": "Browser pool creation failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.get("/browser-pools", summary="List browser pools", response_model=None)
def list_pools(request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    pools = svc.list_pools(ctx)
    return {"pools": [{"id": str(p.id), "name": p.name} for p in pools]}

@router.get("/browser-pools/{pool_id}", summary="Get browser pool", response_model=None)
def get_pool(pool_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService, BrowserPoolNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    try:
        pool = svc.get_pool(ctx, pool_id)
        return {"id": str(pool.id), "name": pool.name}
    except BrowserPoolNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser pool not found",
                "status": 404,
            },
        )

@router.post("/browser-pools/{pool_id}/scale", summary="Scale browser pool", response_model=None)
def scale_pool(pool_id: uuid.UUID, payload: ScaleBrowserPoolRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService, BrowserPoolNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    try:
        svc.scale_pool(ctx, pool_id, target_instances=payload.replicas)
        return {"id": str(pool_id), "scaled": True}
    except BrowserPoolNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser pool not found",
                "status": 404,
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/scale_failed",
                "title": "Scale failed",
                "status": 422,
                "detail": str(exc),
            },
        )

@router.delete("/browser-pools/{pool_id}", summary="Destroy browser pool", response_model=None)
def destroy_pool(pool_id: uuid.UUID, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.domain.context import OrgContext
    from xiosync.services.browser_pools import BrowserPoolService, BrowserPoolNotFoundError

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    svc = BrowserPoolService(session)

    try:
        svc.destroy_pool(ctx, pool_id)
        return {"id": str(pool_id), "destroyed": True}
    except BrowserPoolNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/not_found",
                "title": "Browser pool not found",
                "status": 404,
            },
        )
"""
    with open("xiosync/api/routers/browser_pools.py", "w") as f:
        f.write(content)

rewrite_mesh()
rewrite_compute()
rewrite_browser_sessions()
rewrite_browser_pools()
