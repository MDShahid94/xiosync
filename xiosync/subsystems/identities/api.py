"""Identities API — universal external-identity + credential registry.

RBAC model:
  identities.read   → all GET endpoints (applied at router level in app.py)
  identities.write  → POST, PATCH, DELETE, PUT credential, POST health
  identities.secret → GET credential/{type}/value  (decrypt + return plaintext)

Identities:
    POST   /identities                              — register an identity
    GET    /identities                              — list (filter by platform/state/tags)
    GET    /identities/allocate                     — LRU-pick an active identity for a platform
    GET    /identities/{id}                         — get identity
    PATCH  /identities/{id}                         — update state/display_name/tags/metadata
    DELETE /identities/{id}                         — delete

Credentials:
    PUT    /identities/{id}/credentials/{type}      — store or rotate a credential
    GET    /identities/{id}/credentials             — list credentials (metadata, no values)
    GET    /identities/{id}/credentials/{type}      — get credential metadata
    GET    /identities/{id}/credentials/{type}/value — decrypt and return credential value
    POST   /identities/{id}/credentials/{type}/health — worker updates health score
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/identities", tags=["Identities"])

from xiosync.api.middleware.rbac import require_capability as _rc
_require_write  = _rc("identities.write")  # returns Depends(_dependency) directly
_require_secret = _rc("identities.secret")  # returns Depends(_dependency) directly


# ── Pydantic models ───────────────────────────────────────────────────────────

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateIdentityRequest(_S):
    identifier: str = Field(
        description="Any external identifier: email, username, account ID, handle, phone…"
    )
    platform: str = Field(
        description="External platform: 'google' | 'github' | 'stripe' | 'cloudflare' | 'custom' | …"
    )
    display_name: str | None = None
    state: str = Field(
        default="active",
        description="active | suspended | banned | unverified | expired | archived | …"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    platform_global: bool = Field(
        default=False,
        description="Register as a platform-level shared identity (org-independent)"
    )


class UpdateIdentityRequest(_S):
    model_config = ConfigDict(extra="forbid")
    state: str | None = None
    display_name: str | None = None
    metadata_patch: dict[str, Any] | None = None
    tags: list[str] | None = None


class IdentityResponse(_S):
    model_config = ConfigDict(extra="ignore")
    id: uuid.UUID
    organization_id: uuid.UUID | None
    identifier: str
    platform: str
    display_name: str | None
    state: str
    metadata: dict[str, Any]
    tags: list[str]
    last_used_at: str | None
    created_at: str
    updated_at: str
    is_platform_global: bool


class PutCredentialRequest(_S):
    label: str = Field(
        default="default",
        description="Differentiates multiple credentials of the same type on one identity. "
                    "Use 'default' for the primary; 'sandbox', 'scope:calendar', 'host:vm-01' etc. for extras."
    )
    secret_value: str | None = Field(
        default=None,
        description="Plaintext secret — auto-encrypted into vault. OR supply vault_key."
    )
    vault_key: str | None = Field(
        default=None,
        description="Existing vault key if already stored via /vault/secrets"
    )
    storage_object_key: str | None = Field(
        default=None,
        description="Storage object key for large blobs (certs, profiles) in /storage/objects"
    )
    health_score: float = 1.0
    expires_at: datetime | None = None



class CredentialMetaResponse(_S):
    model_config = ConfigDict(extra="ignore")
    id: uuid.UUID
    identity_id: uuid.UUID
    credential_type: str
    label: str
    vault_key: str | None
    storage_object_key: str | None
    health_score: float
    last_refreshed_at: str | None
    expires_at: str | None
    created_at: str
    updated_at: str



class CredentialValueResponse(_S):
    identity_id: uuid.UUID
    credential_type: str
    value: str


class HealthUpdateRequest(_S):
    health_score: float = Field(ge=0.0, le=1.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _svc(request: Request):
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.subsystems.identities.service import IdentityService
    return IdentityService(cast(OrmSession, request.state.org_session))


def _ctx(request: Request):
    from xiosync.domain.context import OrgContext
    return cast(OrgContext, request.state.org_context)


def _ident_resp(rec) -> IdentityResponse:
    return IdentityResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        identifier=rec.identifier,
        platform=rec.platform,
        display_name=rec.display_name,
        state=rec.state,
        metadata=rec.metadata,
        tags=rec.tags,
        last_used_at=rec.last_used_at.isoformat() if rec.last_used_at else None,
        created_at=rec.created_at.isoformat(),
        updated_at=rec.updated_at.isoformat(),
        is_platform_global=rec.is_platform_global,
    )


def _cred_resp(rec) -> CredentialMetaResponse:
    return CredentialMetaResponse(
        id=rec.id,
        identity_id=rec.identity_id,
        credential_type=rec.credential_type,
        label=rec.label,
        vault_key=rec.vault_key,
        storage_object_key=rec.storage_object_key,
        health_score=rec.health_score,
        last_refreshed_at=rec.last_refreshed_at.isoformat() if rec.last_refreshed_at else None,
        expires_at=rec.expires_at.isoformat() if rec.expires_at else None,
        created_at=rec.created_at.isoformat(),
        updated_at=rec.updated_at.isoformat(),
    )


# ── Identity endpoints ────────────────────────────────────────────────────────

@router.post("", status_code=201, response_model=IdentityResponse,
             summary="Register a new external identity",
             dependencies=[_require_write])
def create_identity(payload: CreateIdentityRequest, request: Request) -> IdentityResponse:
    from xiosync.subsystems.identities.service import IdentityConflictError
    try:
        rec = _svc(request).create_identity(
            _ctx(request),
            identifier=payload.identifier,
            platform=payload.platform,
            display_name=payload.display_name,
            state=payload.state,
            metadata=payload.metadata,
            tags=payload.tags,
            platform_global=payload.platform_global,
        )
    except IdentityConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _ident_resp(rec)


@router.get("", response_model=list[IdentityResponse],
            summary="List identities (filter by platform/state/tags)")
def list_identities(
    request: Request,
    platform: str | None = None,
    state: str | None = None,
    tags: list[str] | None = Query(default=None),
    include_platform: bool = Query(default=True),
    limit: int = Query(default=100, le=500),
) -> list[IdentityResponse]:
    recs = _svc(request).list_identities(
        _ctx(request),
        platform=platform, state=state, tags=tags,
        include_platform=include_platform, limit=limit,
    )
    return [_ident_resp(r) for r in recs]


@router.get("/allocate", response_model=IdentityResponse,
            summary="Allocate the next idle identity for a platform (LRU scheduling)",
            dependencies=[_require_write])
def allocate(
    request: Request,
    platform: str = Query(description="Target platform, e.g. 'google', 'github', 'stripe'"),
    tags: list[str] | None = Query(
        default=None,
        description="Optional tag filter — all listed tags must be present"
    ),
) -> IdentityResponse:
    """Returns the least-recently-used active identity and marks it as used.
    Workers call this to acquire an identity without manual assignment.
    """
    from xiosync.subsystems.identities.service import IdentityNotFoundError
    try:
        rec = _svc(request).allocate(_ctx(request), platform, tags=tags)
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _ident_resp(rec)


@router.post("/allocate", response_model=list[IdentityResponse],
             summary="Batch-allocate N idle identities for a platform (LRU, atomic)",
             dependencies=[_require_write])
def batch_allocate(
    platform: str = Query(..., description="Platform to allocate from: 'google', 'v0', etc."),
    count: int = Query(1, ge=1, le=50, description="Number of identities to allocate (max 50)"),
    tags: list[str] = Query(default=[], description="Required tags (all must match)"),
    request: Request = None,
):
    """Atomically allocate up to `count` identities using LRU + FOR UPDATE SKIP LOCKED.

    Returns fewer than `count` if the pool is exhausted. Use tags to filter
    by capability (e.g. tags=['pro'], tags=['verified']).
    """
    from xiosync.subsystems.identities.service import IdentityService
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy import text as sqlt
    ctx = _ctx(request)
    db  = cast(OrmSession, request.state.org_session)

    tag_filter = ""
    params: dict = {
        "org": str(ctx.organization_id),
        "platform": platform,
        "count": count,
        "state": "active",
    }
    if tags:
        tag_filter = "AND i.tags @> :tags::text[]"
        params["tags"] = tags

    rows = db.execute(
        sqlt(f"""
            WITH locked AS (
                SELECT i.id FROM identities i
                WHERE i.platform = :platform
                  AND i.state    = :state
                  AND (i.organization_id = :org OR i.organization_id IS NULL)
                  {tag_filter}
                ORDER BY i.last_used_at ASC NULLS FIRST
                LIMIT :count
                FOR UPDATE SKIP LOCKED
            )
            UPDATE identities SET last_used_at = now()
            WHERE id IN (SELECT id FROM locked)
            RETURNING id, organization_id, identifier, platform,
                      display_name, state, metadata, tags,
                      last_used_at, created_at, updated_at
        """),
        params,
    ).fetchall()
    db.commit()

    from xiosync.subsystems.identities.service import _row_to_identity
    return [_ident_resp(_row_to_identity(r)) for r in rows]


@router.get("/{identity_id}", response_model=IdentityResponse,
            summary="Get an identity")
def get_identity(identity_id: uuid.UUID, request: Request) -> IdentityResponse:
    from xiosync.subsystems.identities.service import IdentityNotFoundError
    try:
        rec = _svc(request).get_identity(_ctx(request), identity_id)
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail="identity_not_found")
    return _ident_resp(rec)


@router.patch("/{identity_id}", response_model=IdentityResponse,
              summary="Update identity state/display_name/tags/metadata",
              dependencies=[_require_write])
def update_identity(
    identity_id: uuid.UUID, payload: UpdateIdentityRequest, request: Request
) -> IdentityResponse:
    from xiosync.subsystems.identities.service import IdentityNotFoundError
    try:
        rec = _svc(request).update_identity(
            _ctx(request), identity_id,
            state=payload.state,
            display_name=payload.display_name,
            metadata_patch=payload.metadata_patch,
            tags=payload.tags,
        )
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail="identity_not_found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _ident_resp(rec)


@router.delete("/{identity_id}", status_code=204,
               summary="Delete an identity",
               dependencies=[_require_write])
def delete_identity(identity_id: uuid.UUID, request: Request) -> None:
    from xiosync.subsystems.identities.service import IdentityNotFoundError
    try:
        _svc(request).delete_identity(_ctx(request), identity_id)
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail="identity_not_found")


# ── Credential endpoints ──────────────────────────────────────────────────────

@router.put("/{identity_id}/credentials/{credential_type}",
            response_model=CredentialMetaResponse,
            summary="Store or rotate a credential (any type, secret auto-vaulted)",
            dependencies=[_require_write])
def put_credential(
    identity_id: uuid.UUID,
    credential_type: str,
    payload: PutCredentialRequest,
    request: Request,
) -> CredentialMetaResponse:
    """credential_type is free-form: password, api_key, oauth_token, cookie,
    totp_secret, ssh_key, session_token, client_cert, refresh_token, …"""
    from xiosync.subsystems.identities.service import IdentityNotFoundError
    try:
        rec = _svc(request).put_credential(
            _ctx(request), identity_id, credential_type,
            label=payload.label,
            secret_value=payload.secret_value,
            vault_key=payload.vault_key,
            storage_object_key=payload.storage_object_key,
            health_score=payload.health_score,
            expires_at=payload.expires_at,
        )
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail="identity_not_found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _cred_resp(rec)


@router.get("/{identity_id}/credentials",
            response_model=list[CredentialMetaResponse],
            summary="List credentials for an identity (metadata only, never values)")
def list_credentials(identity_id: uuid.UUID, request: Request) -> list[CredentialMetaResponse]:
    recs = _svc(request).list_credentials(_ctx(request), identity_id)
    return [_cred_resp(r) for r in recs]


@router.get("/{identity_id}/credentials/{credential_type}",
            response_model=CredentialMetaResponse,
            summary="Get credential metadata")
def get_credential_meta(
    identity_id: uuid.UUID, credential_type: str, request: Request
) -> CredentialMetaResponse:
    from xiosync.subsystems.identities.service import IdentityNotFoundError
    try:
        rec = _svc(request).get_credential(_ctx(request), identity_id, credential_type)
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail="credential_not_found")
    return _cred_resp(rec)  # type: ignore[arg-type]


@router.get("/{identity_id}/credentials/{credential_type}/value",
            response_model=CredentialValueResponse,
            summary="Decrypt and return a credential value",
            dependencies=[_require_secret])
def get_credential_value(
    identity_id: uuid.UUID, credential_type: str, request: Request
) -> CredentialValueResponse:
    """Returns plaintext credential. Requires identities.secret capability."""
    from xiosync.subsystems.identities.service import IdentityNotFoundError
    try:
        rec, value = _svc(request).get_credential(
            _ctx(request), identity_id, credential_type, decrypt=True
        )
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail="credential_not_found")
    except TypeError:
        raise HTTPException(status_code=422, detail="no_vault_key_set")
    return CredentialValueResponse(
        identity_id=identity_id,
        credential_type=credential_type,
        value=value,
    )


# ── Lease endpoints ───────────────────────────────────────────────────────────

@router.put("/{identity_id}/lease",
            summary="Acquire an exclusive TTL lease on an identity",
            dependencies=[_require_write])
def acquire_lease(
    identity_id: uuid.UUID,
    request: Request,
    duration_seconds: int = Query(default=1800, ge=60, le=86400,
                                   description="Lease TTL in seconds (60s–24h, default 30m)"),
    worker_id: uuid.UUID | None = Query(default=None, description="Enrolling worker UUID"),
    run_context: str = Query(default="{}", description="JSON: {run_id, task_ref, purpose}"),
):
    """Acquire an exclusive time-bounded lease on an identity.

    Use this after GET /allocate to formally claim the identity for the
    duration of a task. If another worker already holds an active lease,
    returns 409 Conflict. Release with DELETE /lease when done.
    """
    import json as _json
    from xiosync.subsystems.identities.service import IdentityNotFoundError
    try:
        ctx = cast(any, _ctx(request))
        lease = _svc(request).acquire_lease(
            ctx, identity_id,
            duration_seconds=duration_seconds,
            worker_id=worker_id,
            run_context=_json.loads(run_context),
        )
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return lease


@router.delete("/{identity_id}/lease/{lease_id}",
               status_code=204,
               summary="Release an identity lease (explicit release)",
               dependencies=[_require_write])
def release_lease(identity_id: uuid.UUID, lease_id: uuid.UUID, request: Request):
    """Explicitly release a lease when a task completes.

    Idempotent — releasing an already-released lease is a no-op.
    """
    _svc(request).release_lease(_ctx(request), lease_id)


@router.get("/{identity_id}/lease",
            summary="List active leases on an identity")
def list_identity_leases(
    identity_id: uuid.UUID, request: Request,
    active_only: bool = Query(default=True),
):
    return _svc(request).list_leases(
        _ctx(request), identity_id=identity_id, active_only=active_only
    )


@router.post("/leases/expire",
             summary="Sweep and mark all expired leases (admin maintenance)",
             dependencies=[_require_write])
def expire_stale_leases(request: Request):
    """Mark all active leases past their TTL as expired.

    Typically called by a cron worker. Returns count of leases swept.
    """
    count = _svc(request).expire_stale_leases(_ctx(request))
    return {"expired_count": count}



@router.post("/{identity_id}/credentials/{credential_type}/health",
             status_code=204,
             summary="Worker updates credential health score",
             dependencies=[_require_write])
def update_health(
    identity_id: uuid.UUID,
    credential_type: str,
    payload: HealthUpdateRequest,
    request: Request,
) -> None:
    _svc(request).update_health(
        _ctx(request), identity_id, credential_type, payload.health_score
    )
