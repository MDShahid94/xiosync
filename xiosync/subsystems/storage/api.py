"""Storage API — provider management + object index.

Providers:
    POST   /storage/providers                       — register or update a provider
    GET    /storage/providers                       — list (org + platform-global)
    GET    /storage/providers/{id}                  — get provider details
    DELETE /storage/providers/{id}                  — delete org-private provider
    GET    /storage/providers/{id}/access/{op}      — get access info for a key

Objects:
    POST   /storage/objects                         — register an uploaded blob
    GET    /storage/objects                         — list (filterable by type/metadata)
    DELETE /storage/objects/{provider_id}/{key}     — deindex an object
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/storage", tags=["Storage"])

# RBAC: storage.read  → all GET endpoints (applied at router level in app.py)
#        storage.write → POST, DELETE (register provider/object, deindex)
from xiosync.api.middleware.rbac import require_capability as _rc
_require_write = _rc("storage.write")  # returns Depends(_dependency) directly


# ── Pydantic models ───────────────────────────────────────────────────────────

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterProviderRequest(_S):
    name: str = Field(description="Label e.g. 'primary', 'cold-backup', 'colab-drive'")
    provider_type: str = Field(
        description="Free-form: 'google_drive' | 'cloudflare_r2' | 's3' | 'local' | 'gcs' | 'azure_blob' | 'custom' | …"
    )
    config: dict[str, Any] = Field(
        description="Non-secret provider config (folder_id, bucket, endpoint, base_path…)"
    )
    vault_key: str | None = Field(
        default=None,
        description="Key in vaulted_secrets holding provider credentials"
    )
    is_default: bool = False
    is_writable: bool = True
    priority: int = Field(default=100, description="Lower = higher priority")
    platform_global: bool = Field(
        default=False,
        description="Register as platform-global shared provider (admin only)"
    )


class ProviderResponse(_S):
    model_config = ConfigDict(extra="ignore")
    id: uuid.UUID
    organization_id: uuid.UUID | None
    name: str
    provider_type: str
    config: dict[str, Any]   # Non-secret config only — credential never returned
    vault_key: str | None
    is_default: bool
    is_writable: bool
    priority: int
    is_platform_global: bool
    created_at: str
    updated_at: str


class RegisterObjectRequest(_S):
    provider_id: uuid.UUID
    object_key: str = Field(description="Logical key within provider (path relative to root)")
    object_type: str = Field(
        default="generic",
        description="'chrome_profile' | 'ts_state' | 'session_export' | 'workflow_artifact' | 'generic'"
    )
    size_bytes: int | None = None
    checksum_sha256: str | None = None
    content_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class ObjectResponse(_S):
    model_config = ConfigDict(extra="ignore")
    id: uuid.UUID
    organization_id: uuid.UUID | None
    provider_id: uuid.UUID
    object_key: str
    object_type: str
    size_bytes: int | None
    checksum_sha256: str | None
    content_type: str | None
    metadata: dict[str, Any]
    expires_at: str | None
    created_at: str
    updated_at: str


class AccessInfoResponse(_S):
    provider_type: str
    operation: str
    url: str | None
    method: str
    headers: dict[str, str] | None
    metadata: dict[str, Any] | None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _svc(request: Request):
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.subsystems.storage.service import StorageService
    return StorageService(cast(OrmSession, request.state.org_session))


def _ctx(request: Request):
    from xiosync.domain.context import OrgContext
    return cast(OrgContext, request.state.org_context)


def _provider_resp(rec) -> ProviderResponse:
    return ProviderResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        name=rec.name,
        provider_type=rec.provider_type,
        config=rec.config,
        vault_key=rec.vault_key,
        is_default=rec.is_default,
        is_writable=rec.is_writable,
        priority=rec.priority,
        is_platform_global=rec.is_platform_global,
        created_at=rec.created_at.isoformat(),
        updated_at=rec.updated_at.isoformat(),
    )


def _object_resp(rec) -> ObjectResponse:
    return ObjectResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        provider_id=rec.provider_id,
        object_key=rec.object_key,
        object_type=rec.object_type,
        size_bytes=rec.size_bytes,
        checksum_sha256=rec.checksum_sha256,
        content_type=rec.content_type,
        metadata=rec.metadata,
        expires_at=rec.expires_at.isoformat() if rec.expires_at else None,
        created_at=rec.created_at.isoformat(),
        updated_at=rec.updated_at.isoformat(),
    )


# ── Provider endpoints ────────────────────────────────────────────────────────

@router.post("/providers", status_code=201, response_model=ProviderResponse,
             summary="Register or update a storage provider",
             dependencies=[_require_write])
def register_provider(payload: RegisterProviderRequest, request: Request) -> ProviderResponse:
    from xiosync.subsystems.storage.service import StorageProviderError
    try:
        rec = _svc(request).register_provider(
            _ctx(request),
            name=payload.name,
            provider_type=payload.provider_type,
            config=payload.config,
            vault_key=payload.vault_key,
            is_default=payload.is_default,
            is_writable=payload.is_writable,
            priority=payload.priority,
            platform_global=payload.platform_global,
        )
    except StorageProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _provider_resp(rec)


@router.get("/providers", response_model=list[ProviderResponse],
            summary="List configured storage providers")
def list_providers(
    request: Request,
    include_platform: bool = Query(default=True),
) -> list[ProviderResponse]:
    recs = _svc(request).list_providers(_ctx(request), include_platform=include_platform)
    return [_provider_resp(r) for r in recs]


@router.get("/providers/{provider_id}", response_model=ProviderResponse,
            summary="Get a storage provider")
def get_provider(provider_id: uuid.UUID, request: Request) -> ProviderResponse:
    from xiosync.subsystems.storage.service import StorageNotFoundError
    try:
        rec = _svc(request).get_provider(_ctx(request), provider_id)
    except StorageNotFoundError:
        raise HTTPException(status_code=404, detail="provider_not_found")
    return _provider_resp(rec)


@router.delete("/providers/{provider_id}", status_code=204,
               summary="Delete an org-private storage provider",
               dependencies=[_require_write])
def delete_provider(provider_id: uuid.UUID, request: Request) -> None:
    from xiosync.subsystems.storage.service import StorageNotFoundError
    try:
        _svc(request).delete_provider(_ctx(request), provider_id)
    except StorageNotFoundError:
        raise HTTPException(status_code=404, detail="provider_not_found")


@router.get("/providers/{provider_id}/access/{operation}",
            response_model=AccessInfoResponse,
            summary="Get access instructions for a blob (upload/download/delete)")
def get_access_info(
    provider_id: uuid.UUID,
    operation: str,
    request: Request,
    object_key: str = Query(description="Object key within the provider"),
) -> AccessInfoResponse:
    from xiosync.subsystems.storage.service import StorageNotFoundError, StorageProviderError
    if operation not in ("upload", "download", "delete"):
        raise HTTPException(status_code=422, detail="operation must be upload|download|delete")
    try:
        info = _svc(request).get_access_info(_ctx(request), provider_id, object_key, operation)
    except StorageNotFoundError:
        raise HTTPException(status_code=404, detail="provider_not_found")
    except StorageProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return AccessInfoResponse(
        provider_type=info.provider_type,
        operation=info.operation,
        url=info.url,
        method=info.method,
        headers=info.headers,
        metadata=info.metadata,
    )


# ── Object endpoints ──────────────────────────────────────────────────────────

@router.post("/objects", status_code=201, response_model=ObjectResponse,
             summary="Register an uploaded blob in the object index",
             dependencies=[_require_write])
def register_object(payload: RegisterObjectRequest, request: Request) -> ObjectResponse:
    """Workers call this AFTER uploading a blob to the provider to index it in XIOSYNC."""
    from xiosync.subsystems.storage.service import StorageProviderError, StorageNotFoundError
    try:
        rec = _svc(request).register_object(
            _ctx(request),
            provider_id=payload.provider_id,
            object_key=payload.object_key,
            object_type=payload.object_type,
            size_bytes=payload.size_bytes,
            checksum_sha256=payload.checksum_sha256,
            content_type=payload.content_type,
            metadata=payload.metadata,
            expires_at=payload.expires_at,
        )
    except (StorageProviderError, StorageNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _object_resp(rec)


@router.get("/objects", response_model=list[ObjectResponse],
            summary="List objects in the index")
def list_objects(
    request: Request,
    provider_id: uuid.UUID | None = None,
    object_type: str | None = None,
    limit: int = Query(default=50, le=200),
) -> list[ObjectResponse]:
    recs = _svc(request).list_objects(
        _ctx(request), provider_id=provider_id, object_type=object_type, limit=limit
    )
    return [_object_resp(r) for r in recs]


@router.delete("/objects/{provider_id}/{object_key:path}", status_code=204,
               summary="Remove an object from the index (does NOT delete from provider)",
               dependencies=[_require_write])
def deindex_object(
    provider_id: uuid.UUID,
    object_key: str,
    request: Request,
) -> None:
    from xiosync.subsystems.storage.service import StorageNotFoundError
    try:
        _svc(request).deindex_object(_ctx(request), provider_id, object_key)
    except StorageNotFoundError:
        raise HTTPException(status_code=404, detail="object_not_found")
