"""Vault API — universal secret store endpoints.

Secrets are org-scoped by default. Platform-global secrets (organization_id=NULL)
are shared across all orgs and require the 'vault.platform' capability to write.

Endpoints:
    POST   /vault/secrets                  — store or rotate a secret
    GET    /vault/secrets                  — list keys (no values)
    GET    /vault/secrets/{key}            — retrieve plaintext value
    GET    /vault/secrets/{key}/meta       — metadata only (no plaintext)
    DELETE /vault/secrets/{key}            — delete
    POST   /vault/secrets/{key}/rotate     — re-encrypt with fresh IV
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/vault", tags=["Vault"])


# ── Request / Response models ─────────────────────────────────────────────────

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PutSecretRequest(_S):
    key: str = Field(description="Namespaced key e.g. 'cf_api_token' or 'totp/user@gmail.com'")
    value: str = Field(description="Plaintext secret value")
    secret_type: str = Field(
        default="generic",
        description="'generic' | 'credential' | 'token' | 'totp' | 'api_key' | 'ssh_key' | 'oauth'"
    )
    description: str | None = None
    expires_at: datetime | None = None
    platform_global: bool = Field(
        default=False,
        description="If true, stores as a platform-level shared secret (admin only)"
    )


class SecretMetaResponse(_S):
    model_config = ConfigDict(extra="ignore")
    id: uuid.UUID
    organization_id: uuid.UUID | None   # None = platform-global
    key: str
    secret_type: str
    description: str | None = None
    expires_at: str | None = None
    rotated_at: str | None = None
    created_at: str
    updated_at: str
    is_platform_global: bool


class SecretValueResponse(_S):
    key: str
    value: str
    secret_type: str
    organization_id: uuid.UUID | None
    is_platform_global: bool


def _svc(request: Request):
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.subsystems.vault.service import VaultService
    return VaultService(cast(OrmSession, request.state.org_session))


def _ctx(request: Request):
    from xiosync.domain.context import OrgContext
    return cast(OrgContext, request.state.org_context)


def _meta_resp(rec) -> SecretMetaResponse:
    return SecretMetaResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        key=rec.key,
        secret_type=rec.secret_type,
        description=rec.description,
        expires_at=rec.expires_at.isoformat() if rec.expires_at else None,
        rotated_at=rec.rotated_at.isoformat() if rec.rotated_at else None,
        created_at=rec.created_at.isoformat(),
        updated_at=rec.updated_at.isoformat(),
        is_platform_global=(rec.organization_id is None),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/secrets", status_code=201, response_model=SecretMetaResponse,
             summary="Store or rotate a secret (upsert)")
def put_secret(payload: PutSecretRequest, request: Request) -> SecretMetaResponse:
    """Store or rotate a secret. Platform-global secrets require vault.platform capability."""
    from xiosync.subsystems.vault.service import VaultNotFoundError
    svc = _svc(request)
    ctx = _ctx(request)
    try:
        rec = svc.put_secret(
            ctx, payload.key, payload.value,
            secret_type=payload.secret_type,
            description=payload.description,
            expires_at=payload.expires_at,
            platform_global=payload.platform_global,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _meta_resp(rec)


@router.get("/secrets", response_model=list[SecretMetaResponse],
            summary="List secret keys (no values, ever)")
def list_secrets(
    request: Request,
    secret_type: str | None = None,
    include_platform: bool = Query(default=True, description="Include platform-global secrets"),
) -> list[SecretMetaResponse]:
    recs = _svc(request).list_secrets(
        _ctx(request), secret_type=secret_type, include_platform=include_platform
    )
    return [_meta_resp(r) for r in recs]


@router.get("/secrets/{key}/meta", response_model=SecretMetaResponse,
            summary="Get metadata for a secret (no plaintext)")
def get_secret_meta(key: str, request: Request) -> SecretMetaResponse:
    from xiosync.subsystems.vault.service import VaultNotFoundError
    try:
        rec = _svc(request).get_meta(_ctx(request), key)
    except VaultNotFoundError:
        raise HTTPException(status_code=404, detail="secret_not_found")
    return _meta_resp(rec)


@router.get("/secrets/{key}", response_model=SecretValueResponse,
            summary="Retrieve a decrypted secret value")
def get_secret(
    key: str,
    request: Request,
    allow_platform: bool = Query(default=True),
) -> SecretValueResponse:
    """Returns the plaintext secret. Requires vault.read capability."""
    from xiosync.subsystems.vault.service import VaultNotFoundError, VaultCryptoError
    svc = _svc(request)
    ctx = _ctx(request)
    try:
        value = svc.get_secret(ctx, key, allow_platform=allow_platform)
        meta  = svc.get_meta(ctx, key)
    except VaultNotFoundError:
        raise HTTPException(status_code=404, detail="secret_not_found")
    except VaultCryptoError as exc:
        raise HTTPException(status_code=500, detail=f"decryption_failed: {exc}")
    return SecretValueResponse(
        key=key,
        value=value,
        secret_type=meta.secret_type,
        organization_id=meta.organization_id,
        is_platform_global=(meta.organization_id is None),
    )


@router.delete("/secrets/{key}", status_code=204,
               summary="Permanently delete a secret")
def delete_secret(key: str, request: Request) -> None:
    from xiosync.subsystems.vault.service import VaultNotFoundError
    try:
        _svc(request).delete_secret(_ctx(request), key)
    except VaultNotFoundError:
        raise HTTPException(status_code=404, detail="secret_not_found")


@router.post("/secrets/{key}/rotate", response_model=SecretMetaResponse,
             summary="Re-encrypt a secret with a fresh IV (key rotation)")
def rotate_secret(key: str, request: Request) -> SecretMetaResponse:
    """Re-encrypts with a fresh random IV — useful for periodic rotation policy."""
    from xiosync.subsystems.vault.service import VaultNotFoundError, VaultCryptoError
    svc = _svc(request)
    ctx = _ctx(request)
    try:
        # Read then re-write — triggers the ON CONFLICT rotated_at = now()
        value = svc.get_secret(ctx, key)
        meta  = svc.get_meta(ctx, key)
        rec   = svc.put_secret(
            ctx, key, value,
            secret_type=meta.secret_type,
            description=meta.description,
            expires_at=meta.expires_at,
            platform_global=(meta.organization_id is None),
        )
    except VaultNotFoundError:
        raise HTTPException(status_code=404, detail="secret_not_found")
    except VaultCryptoError as exc:
        raise HTTPException(status_code=500, detail=f"rotation_failed: {exc}")
    return _meta_resp(rec)
