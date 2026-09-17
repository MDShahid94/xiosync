"""Integrations API + Worker Config endpoints.

Integrations:
    POST   /integrations/providers                  — register any external connector
    GET    /integrations/providers                  — list
    GET    /integrations/providers/{id}             — get
    DELETE /integrations/providers/{id}             — delete
    POST   /integrations/providers/{id}/test        — test connection

Worker Config:
    GET    /workers/{id}/config                     — worker pulls config
    PUT    /workers/{id}/config                     — admin pushes config
"""
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

integrations_router  = APIRouter(prefix="/integrations", tags=["Integrations"])
worker_config_router = APIRouter(prefix="/workers",      tags=["Workers"])


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIntegrationRequest(_S):
    name: str
    provider_type: str = Field(
        description="Free-form: 'cloudflare_d1' | 'postgres' | 'redis' | 'rest_api' | 'custom' | …"
    )
    connection_config: dict[str, Any] = Field(
        description="Non-secret parameters: host, port, database, account_id, …"
    )
    vault_key: str | None = Field(
        default=None, description="Key in vaulted_secrets holding the connection credential"
    )


class IntegrationResponse(_S):
    model_config = ConfigDict(extra="ignore")
    id: uuid.UUID
    organization_id: uuid.UUID | None
    name: str
    provider_type: str
    connection_config: dict[str, Any]
    vault_key: str | None
    last_synced_at: str | None
    last_sync_status: str | None
    sync_stats: dict[str, Any]
    created_at: str
    updated_at: str


class WorkerConfigRequest(_S):
    config: dict[str, Any] = Field(description="Non-sensitive runtime config for the worker")


def _isvc(request: Request):
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.subsystems.integrations.service import IntegrationsService
    return IntegrationsService(cast(OrmSession, request.state.org_session))


def _ctx(request: Request):
    from xiosync.domain.context import OrgContext
    return cast(OrgContext, request.state.org_context)


def _int_resp(rec) -> IntegrationResponse:
    return IntegrationResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        name=rec.name,
        provider_type=rec.provider_type,
        connection_config=rec.connection_config,
        vault_key=rec.vault_key,
        last_synced_at=rec.last_synced_at.isoformat() if rec.last_synced_at else None,
        last_sync_status=rec.last_sync_status,
        sync_stats=rec.sync_stats,
        created_at=rec.created_at.isoformat(),
        updated_at=rec.updated_at.isoformat(),
    )


# ── Integration endpoints ─────────────────────────────────────────────────────

@integrations_router.post("/providers", status_code=201, response_model=IntegrationResponse,
                          summary="Register an external connector (any provider type)")
def register_integration(payload: RegisterIntegrationRequest, request: Request):
    try:
        rec = _isvc(request).register(
            _ctx(request), payload.name, payload.provider_type,
            payload.connection_config, vault_key=payload.vault_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _int_resp(rec)


@integrations_router.get("/providers", response_model=list[IntegrationResponse],
                         summary="List integration providers")
def list_integrations(request: Request):
    return [_int_resp(r) for r in _isvc(request).list_providers(_ctx(request))]


@integrations_router.get("/providers/{provider_id}", response_model=IntegrationResponse,
                         summary="Get an integration provider")
def get_integration(provider_id: uuid.UUID, request: Request):
    from xiosync.subsystems.integrations.service import IntegrationNotFoundError
    try:
        return _int_resp(_isvc(request).get_provider(_ctx(request), provider_id))
    except IntegrationNotFoundError:
        raise HTTPException(status_code=404, detail="integration_not_found")


@integrations_router.delete("/providers/{provider_id}", status_code=204,
                            summary="Delete an integration provider")
def delete_integration(provider_id: uuid.UUID, request: Request):
    from xiosync.subsystems.integrations.service import IntegrationNotFoundError
    try:
        _isvc(request).delete_provider(_ctx(request), provider_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=404, detail="integration_not_found")


@integrations_router.post("/providers/{provider_id}/test",
                          summary="Test connection to an integration provider")
def test_connection(provider_id: uuid.UUID, request: Request):
    from xiosync.subsystems.integrations.service import IntegrationNotFoundError
    try:
        return _isvc(request).test_connection(_ctx(request), provider_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=404, detail="integration_not_found")


@integrations_router.post("/providers/{provider_id}/sync-result",
                          summary="Record the outcome of an external sync run")
def record_sync_result(provider_id: uuid.UUID, request: Request,
                       status: str = "ok", stats: dict = None):
    """Called by migration tools or adapters after completing a sync.

    Updates last_synced_at, last_sync_status and sync_stats on the provider record.
    """
    import json as _json
    from sqlalchemy import text as sqlt
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.subsystems.integrations.service import IntegrationNotFoundError
    db  = cast(OrmSession, request.state.org_session)
    ctx = _ctx(request)
    result = db.execute(
        sqlt("""
            UPDATE integration_providers
            SET last_synced_at    = now(),
                last_sync_status  = :status,
                sync_stats        = cast(:stats as jsonb),
                updated_at        = now()
            WHERE id = :id AND organization_id = :org
            RETURNING id, last_synced_at, last_sync_status
        """),
        {"id": str(provider_id), "org": str(ctx.organization_id),
         "status": status, "stats": _json.dumps(stats or {})},
    ).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="integration_not_found")
    db.commit()
    return {
        "provider_id": str(result.id),
        "last_synced_at": result.last_synced_at.isoformat(),
        "last_sync_status": result.last_sync_status,
    }


# ── Worker config endpoints ───────────────────────────────────────────────────

@worker_config_router.get("/{worker_id}/config", summary="Worker pulls its runtime config")
def get_worker_config(worker_id: uuid.UUID, request: Request):
    """Workers call this on startup to receive their full runtime configuration."""
    import json
    from sqlalchemy import text as sqlt
    from sqlalchemy.orm import Session as OrmSession
    db  = cast(OrmSession, request.state.org_session)
    ctx = _ctx(request)
    row = db.execute(
        sqlt("SELECT id, config FROM worker_enrollments WHERE id=:id AND organization_id=:org"),
        {"id": str(worker_id), "org": str(ctx.organization_id)},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="worker_not_found")
    cfg = row.config if isinstance(row.config, dict) else json.loads(row.config or "{}")
    return {"worker_id": str(row.id), "config": cfg}


@worker_config_router.put("/{worker_id}/config", status_code=200,
                          summary="Admin pushes runtime config to a worker")
def put_worker_config(worker_id: uuid.UUID, payload: WorkerConfigRequest, request: Request):
    """Admin sets worker config. Workers pick it up on next restart via GET."""
    import json
    from sqlalchemy import text as sqlt
    from sqlalchemy.orm import Session as OrmSession
    db  = cast(OrmSession, request.state.org_session)
    ctx = _ctx(request)
    result = db.execute(
        sqlt("""
            UPDATE worker_enrollments SET config=cast(:cfg as jsonb), updated_at=now()
            WHERE id=:id AND organization_id=:org RETURNING id
        """),
        {"id": str(worker_id), "org": str(ctx.organization_id),
         "cfg": json.dumps(payload.config)},
    ).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="worker_not_found")
    db.commit()
    return {"worker_id": str(result.id), "config": payload.config}
