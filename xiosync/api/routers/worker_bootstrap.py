"""Worker bootstrap token API.

Provides a secure, one-time (or reusable) bootstrap URL that Colab workers
fetch to get their full runtime CONFIG. This eliminates all hardcoded secrets
from the Colab notebook.

Flow:
  Admin:  POST /api/v1/workers/bootstrap-tokens  →  {token, url}
  Colab:  GET  /api/v1/workers/bootstrap/{token} →  {config: {...}}
          (no auth header required — token IS the credential)

The delivered config contains everything boot.py needs:
  - xiosync_url, xiosync_token, xiosync_worker_secret
  - xiosync_internal_secret
  - tailscale_auth_key
  - r2_endpoint, r2_bucket, r2_access_key, r2_secret_key
  - node_name, xiorun_agent_port, local_root

Config is assembled by XIOSYNC from:
  1. platform-level vault secrets (tailscale key, R2 creds, internal secret)
  2. worker-specific overrides (node_name, caps)

Bootstrap tokens are stored in vaulted_secrets as:
  key:  workers/bootstrap/{token_hash}
  org:  Org Zero
  value: JSON config blob

Tokens can be:
  - one-shot: deleted after first use (use_count incremented)
  - reusable: valid until revoked (useful for auto-spawn flows)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["worker-bootstrap"])

_ORG_ZERO = uuid.UUID("00000000-0000-7000-8000-000000000000")

# ── Pydantic models ────────────────────────────────────────────────────────────

class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _make_ctx():
    """Return a platform-admin OrgContext for Org Zero bootstrap operations."""
    from xiosync.domain.context import (  # noqa: PLC0415
        OrgContext, PlatformRole, MembershipRole,
    )
    return OrgContext(
        auth_identity_id=_ORG_ZERO,
        actor_id=_ORG_ZERO,
        organization_id=_ORG_ZERO,
        session_id=_ORG_ZERO,
        platform_role=PlatformRole.PLATFORM_ADMIN,
        membership_role=MembershipRole.ORG_OWNER,
    )


class CreateBootstrapTokenRequest(_S):
    org_slug: str = Field(
        default="xiogrid",
        description="Organization slug. Becomes the first segment of the runtime name.",
        pattern=r"^[a-z0-9][a-z0-9\-]{0,48}[a-z0-9]$",
    )
    project_slug: str = Field(
        default="default",
        description="Project slug within the org. Becomes the second segment.",
        pattern=r"^[a-z0-9][a-z0-9\-]{0,48}[a-z0-9]$",
    )
    role: str = Field(
        default="worker",
        description=(
            "Runtime role: 'master' (orchestrator), 'worker' (task runner), "
            "or any custom slug. Becomes the third segment."
        ),
        pattern=r"^[a-z0-9][a-z0-9\-]{0,32}[a-z0-9]$",
    )
    reusable: bool = Field(
        default=True,
        description="If False, token is deleted after first use.",
    )
    note: str = Field(
        default="",
        description="Human-readable note (displayed in admin list).",
    )
    extra_config: dict = Field(
        default_factory=dict,
        description="Extra key/value pairs merged into the delivered config.",
    )


class CreateBootstrapTokenResponse(_S):
    model_config = ConfigDict(extra="ignore")
    token:     str
    colab_url: str
    note:      str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _token_vault_key(token_hash: str) -> str:
    return f"workers/bootstrap/{token_hash}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _assemble_worker_config(
    session,
    node_name: str,
    extra_config: dict,
    xiosync_base: str,
    *,
    org_slug: str = "xiogrid",
    project_slug: str = "default",
    role: str = "worker",
) -> dict:
    """Assemble the full CONFIG dict delivered to a Colab worker.

    Reads all secrets from the XIOSYNC vault (Org Zero) so no secret
    ever appears in notebook code or git history.
    """
    from xiosync.subsystems.vault.service import VaultService  # noqa: PLC0415


    ctx = _make_ctx()
    vault = VaultService(session)

    def _safe_get(key: str, default: str = "") -> str:
        try:
            return vault.get_secret(ctx, key, allow_platform=True)
        except Exception:
            return default

    # Generate a bearer token for this worker — use worker org secret for now
    # (a proper scoped worker JWT is a future improvement)
    worker_secret = _safe_get("platform/worker_org_secret")
    internal_secret = _safe_get("platform/internal_secret")
    ts_auth_key = _safe_get("platform/tailscale_auth_key")
    ts_exit_ip = _safe_get("platform/tailscale_exit_ip")
    ssh_authorized_key = _safe_get("platform/ssh_authorized_key")

    # R2 credentials — stored as JSON blob at storage/r2_primary
    _r2_blob = _safe_get("storage/r2_primary")
    try:
        _r2 = json.loads(_r2_blob) if _r2_blob else {}
    except Exception:
        _r2 = {}
    r2_access_key = _r2.get("access_key_id", "")
    r2_secret_key = _r2.get("secret_access_key", "")

    # R2 endpoint/bucket from storage_providers table
    from sqlalchemy import text  # noqa: PLC0415
    r2_row = session.execute(
        text("""
            SELECT config->>'endpoint' AS endpoint,
                   config->>'bucket'   AS bucket
            FROM storage_providers
            WHERE organization_id = :org AND provider_type = 'cloudflare_r2'
            LIMIT 1
        """),
        {"org": str(_ORG_ZERO)},
    ).mappings().first()

    # Drive provider config — for FUSE mount and shortcut setup
    drive_row = session.execute(
        text("""
            SELECT config->>'folder_id'     AS folder_id,
                   config->>'shortcut_name' AS shortcut_name
            FROM storage_providers
            WHERE provider_type = 'google_drive'
              AND is_default = true
            LIMIT 1
        """),
    ).mappings().first()

    _drive_folder_id     = (drive_row["folder_id"]     if drive_row else "") or "19k79lkPzg1gBM7IhIhE-35rfiAyVfCsK"
    _drive_shortcut_name = (drive_row["shortcut_name"] if drive_row else "") or "XIOSYNC-Shared"
    _drive_fs_root       = f"/content/drive/MyDrive/{_drive_shortcut_name}"

    config: dict = {
        "node_name":                node_name,
        # Stable identity without counter suffix (e.g. "xiogrid--default--master")
        # Workers use this as their TS identity, Drive key prefix, etc.
        "node_identity":            re.sub(r"-\d{3,}$", "", node_name),
        # Org/project/role context — carried through to workers and XIORUN
        "org_slug":                 org_slug,
        "project_slug":             project_slug,
        "role":                     role,
        "xiosync_url":              xiosync_base,
        "xiosync_token":            worker_secret,   # workers use org secret as bearer
        "xiosync_worker_secret":    worker_secret,
        "xiosync_internal_secret":  internal_secret,
        "tailscale_auth_key":       ts_auth_key,
        "default_exit":             ts_exit_ip,
        "ssh_authorized_key":       ssh_authorized_key,
        "r2_endpoint":              r2_row["endpoint"] if r2_row else "",
        "r2_bucket":                r2_row["bucket"]   if r2_row else "xio-profiles",
        "r2_access_key":            r2_access_key,
        "r2_secret_key":            r2_secret_key,
        "xiorun_agent_port":        9300,
        "local_root":               "/content/xiosync-worker",
        # Drive FUSE mount configuration
        "drive_folder_id":          _drive_folder_id,
        "drive_shortcut_name":      _drive_shortcut_name,
        "drive_fs_root":            _drive_fs_root,
        **extra_config,
    }
    return config


def _get_xiosync_base(request: Request) -> str:
    """Derive the XIOSYNC base URL to embed in worker configs.

    Priority:
      1. XIOSYNC_PUBLIC_URL env var (set by admin for Tailscale/tunnel access)
      2. Constructed from request.base_url (works for same-network access)
    """
    return os.environ.get(
        "XIOSYNC_PUBLIC_URL",
        str(request.base_url).rstrip("/"),
    )


# ── Admin endpoints ────────────────────────────────────────────────────────────

@router.post(
    "/workers/bootstrap-tokens",
    summary="[Admin] Generate a worker bootstrap token",
    status_code=201,
)
def create_bootstrap_token(
    payload: CreateBootstrapTokenRequest,
    request: Request,
) -> dict:
    """Generate a single-URL bootstrap credential for a Colab worker.

    The returned `colab_url` can be opened directly — it embeds the token
    and opens the notebook in playground (sandbox) mode so workers cannot
    accidentally edit it.

    Secrets are stored in the XIOSYNC vault, not in the token itself.
    The token is only a lookup key.
    """
    from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415
    from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415
    from xiosync.subsystems.vault.service import VaultService  # noqa: PLC0415


    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    vault_key  = _token_vault_key(token_hash)

    xiosync_base = _get_xiosync_base(request)

    # Derive the base node_name from the three semantic segments.
    # Double-dash separator avoids collision with single-dash slugs (e.g. "acme-corp").
    # boot.py strips the trailing counter suffix to get NODE_IDENTITY.
    _node_base = f"{payload.org_slug}--{payload.project_slug}--{payload.role}"

    meta = {
        "node_name":    _node_base,
        "org_slug":     payload.org_slug,
        "project_slug": payload.project_slug,
        "role":         payload.role,
        "reusable":     payload.reusable,
        "note":         payload.note,
        "extra_config": payload.extra_config,
        "created_by":   str(getattr(getattr(request.state, "org_context", None), "actor_id", "")),
        "use_count":    0,
    }

    with OrmSession(get_engine()) as sess:
        ctx = _make_ctx()
        VaultService(sess).put_secret(
            ctx,
            vault_key,
            json.dumps(meta),
            secret_type="generic",
            platform_global=True,   # bootstrap tokens are global — fetched by public endpoint
        )
        sess.commit()

    # Get the notebook Drive file ID from Org Zero's default storage provider
    with OrmSession(get_engine()) as sess:
        nb_row = sess.execute(
            text("""
                SELECT config->>'notebook_file_id' AS file_id
                FROM storage_providers
                WHERE organization_id = :org AND provider_type = 'google_drive'
                  AND is_default = true
                LIMIT 1
            """),
            {"org": str(_ORG_ZERO)},
        ).mappings().first()

    nb_file_id = (nb_row["file_id"] if nb_row and nb_row["file_id"] else "")
    colab_base = f"https://colab.research.google.com/drive/{nb_file_id}" if nb_file_id else \
                 "https://colab.research.google.com"
    colab_url = (
        f"{colab_base}"
        f"?xiosync_base={xiosync_base}&bootstrap_token={token}"
        f"#forceEdit=true&sandboxMode=true"
    )

    logger.info("worker_bootstrap.created", extra={
        "node_name": _node_base, "org_slug": payload.org_slug,
        "project_slug": payload.project_slug, "role": payload.role,
        "reusable": payload.reusable,
    })
    return {
        "token":     token,
        "colab_url": colab_url,
        "note":      payload.note,
        "reusable":  payload.reusable,
    }


# ── Public endpoint (no auth — token is the credential) ───────────────────────

@router.get(
    "/workers/bootstrap/{token}",
    summary="[Colab] Fetch worker runtime config via bootstrap token",
    include_in_schema=True,
)
def fetch_bootstrap_config(token: str, request: Request) -> dict:
    """Called by boot.py at Colab startup to get the full CONFIG dict.

    No Authorization header required — the token itself is the credential.
    On first use of a one-shot token, the token is deleted.
    """
    from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415
    from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415
    from xiosync.subsystems.vault.service import VaultService, VaultNotFoundError  # noqa: PLC0415


    token_hash = _hash_token(token)
    vault_key  = _token_vault_key(token_hash)
    ctx = _make_ctx()

    with OrmSession(get_engine()) as sess:
        vault = VaultService(sess)
        try:
            raw = vault.get_secret(ctx, vault_key, allow_platform=True)
        except Exception:
            raise HTTPException(status_code=404, detail="Invalid or expired bootstrap token")

        meta = json.loads(raw)
        reusable     = meta.get("reusable", True)
        node_name    = meta.get("node_name", "xiogrid--default--worker")
        org_slug     = meta.get("org_slug", "xiogrid")
        project_slug = meta.get("project_slug", "default")
        role         = meta.get("role", "worker")
        extra_cfg    = meta.get("extra_config", {})
        use_count    = meta.get("use_count", 0)

        # ── Node suffix strategy ───────────────────────────────────────────────
        # Single-instance roles (master, main, primary…) always get suffix -001
        # so the node name stays stable across reboots.
        # Multi-instance roles (worker, agent, runner…) increment per use so
        # multiple simultaneous instances get unique names.
        #
        # Token metadata can also set max_instances=1 to force stable naming
        # regardless of role name.
        _SINGLE_INSTANCE_ROLES = {"master", "main", "primary", "solo", "single", "control"}
        max_instances = meta.get("max_instances", None)
        _is_single = (
            max_instances == 1
            or (max_instances is None and role.lower() in _SINGLE_INSTANCE_ROLES)
        )

        if reusable:
            if _is_single:
                # Stable: always -001 — reboots don't change the node identity
                node_name = f"{node_name}-001"
            elif use_count > 0:
                # Multi-instance: new suffix each use → parallel workers get unique names
                node_name = f"{node_name}-{use_count + 1:03d}"
        elif not reusable and use_count > 0:
            raise HTTPException(status_code=410, detail="Bootstrap token already used")

        # Update use count (always, for auditing)
        meta["use_count"] = use_count + 1
        vault.put_secret(ctx, vault_key, json.dumps(meta),
                         secret_type="generic", platform_global=False)

        # One-shot: delete after use
        if not reusable:
            vault.delete_secret(ctx, vault_key)

        xiosync_base = _get_xiosync_base(request)
        config = _assemble_worker_config(
            sess, node_name, extra_cfg, xiosync_base,
            org_slug=org_slug, project_slug=project_slug, role=role,
        )
        sess.commit()

    logger.info("worker_bootstrap.fetched", extra={
        "node_name": node_name, "reusable": reusable, "use_count": use_count + 1,
    })
    return {"config": config, "node_name": node_name}


@router.get(
    "/workers/bootstrap-tokens",
    summary="[Admin] List active bootstrap tokens",
)
def list_bootstrap_tokens(request: Request) -> dict:
    """List all active bootstrap tokens (without revealing the token itself)."""
    from sqlalchemy import text  # noqa: PLC0415
    from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415
    from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

    with OrmSession(get_engine()) as sess:
        rows = sess.execute(
            text("""
                SELECT key, created_at, updated_at
                FROM vaulted_secrets
                WHERE organization_id = :org
                  AND key LIKE 'workers/bootstrap/%'
                ORDER BY created_at DESC
                LIMIT 100
            """),
            {"org": str(_ORG_ZERO)},
        ).mappings().all()

    return {
        "tokens": [
            {
                "key_suffix": row["key"].removeprefix("workers/bootstrap/")[:12] + "…",
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.get(
    "/workers/boot.py",
    summary="[Public] Download the latest boot.py for Colab workers",
    include_in_schema=True,
)
def serve_boot_py():
    """Serve boot.py directly from XIOSYNC — no GitHub dependency.

    No auth required — the file contains no secrets.
    Workers always get the latest version on every boot.
    """
    import pathlib
    from fastapi.responses import PlainTextResponse
    _root = pathlib.Path(__file__).parent.parent.parent.parent
    boot_path = _root / "colab" / "boot.py"
    if not boot_path.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="boot.py not found on server")
    return PlainTextResponse(boot_path.read_text())


@router.get(
    "/workers/xiorun-agent.py",
    summary="[Public] Download the latest xiorun_agent.py for Colab workers",
    include_in_schema=True,
)
def serve_xiorun_agent():
    """Serve xiorun_agent.py directly from XIOSYNC — no GitHub dependency."""
    import pathlib
    from fastapi.responses import PlainTextResponse
    _root = pathlib.Path(__file__).parent.parent.parent.parent
    agent_path = _root / "colab" / "xiorun_agent.py"
    if not agent_path.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="xiorun_agent.py not found on server")
    return PlainTextResponse(agent_path.read_text())


@router.get(
    "/workers/xio-drive-fs.py",
    summary="[Public] Download xio_drive_fs.py — Drive FUSE accessor utility",
    include_in_schema=True,
)
def serve_xio_drive_fs():
    """Serve xio_drive_fs.py from XIOSYNC.

    Workers fetch this once at boot to get the Drive FUSE mount helper
    and XIODriveFS class (with distributed locking + dedup).
    No auth required — the file contains no secrets.
    """
    import pathlib
    from fastapi.responses import PlainTextResponse
    _root = pathlib.Path(__file__).parent.parent.parent.parent
    fs_path = _root / "colab" / "xio_drive_fs.py"
    if not fs_path.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="xio_drive_fs.py not found on server")
    return PlainTextResponse(fs_path.read_text())


@router.get(
    "/workers/notebook.ipynb",
    summary="[Public] Download the latest xiosync-worker.ipynb notebook",
    include_in_schema=True,
)
def serve_notebook():
    """Serve xiosync-worker.ipynb from XIOSYNC — canonical notebook for spawning workers.

    No auth required. Workers fetch this on boot to self-update their Drive copy.
    Binary download (application/json wrapped as .ipynb).
    """
    import pathlib, hashlib  # noqa: PLC0415
    from fastapi.responses import Response  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415
    _root = pathlib.Path(__file__).parent.parent.parent.parent
    nb_path = _root / "colab" / "xiosync-worker.ipynb"
    if not nb_path.exists():
        raise HTTPException(status_code=404, detail="xiosync-worker.ipynb not found on server")
    data = nb_path.read_bytes()
    sha  = hashlib.sha256(data).hexdigest()
    return Response(
        content=data,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="xiosync-worker.ipynb"',
            "X-Notebook-SHA256": sha,
        },
    )


@router.get(
    "/workers/notebook-hash",
    summary="[Public] SHA-256 hash of the current xiosync-worker.ipynb",
    include_in_schema=True,
)
def serve_notebook_hash():
    """Return SHA-256 of the current notebook for quick update checks.

    Workers call this first (tiny payload) before deciding whether to
    download the full notebook. No auth required.
    """
    import pathlib, hashlib, datetime  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415
    _root = pathlib.Path(__file__).parent.parent.parent.parent
    nb_path = _root / "colab" / "xiosync-worker.ipynb"
    if not nb_path.exists():
        raise HTTPException(status_code=404, detail="xiosync-worker.ipynb not found on server")
    data = nb_path.read_bytes()
    sha  = hashlib.sha256(data).hexdigest()
    mtime = datetime.datetime.fromtimestamp(
        nb_path.stat().st_mtime, tz=datetime.timezone.utc
    ).isoformat()
    return JSONResponse({"sha256": sha, "updated_at": mtime, "name": "xiosync-worker.ipynb"})


# ── Worker self-enroll (public — authenticates via worker_org_secret) ─────────

class _SelfEnrollReq(_S):
    worker_org_secret: str
    runtime_type: str = "colab"
    tailscale_ip: str | None = None
    reported_caps: list[str] = []
    software_version: str | None = None
    public_key: str = ""


@router.post(
    "/workers/self-enroll",
    status_code=201,
    summary="[Public] Autonomous worker self-enrollment via org secret",
    response_model=None,
)
def self_enroll(payload: _SelfEnrollReq):
    """Enroll a Colab/VM worker using XIOSYNC_WORKER_ORG_SECRET.

    No Bearer token needed — the shared org secret IS the credential.
    Returns enrollment_id + enrollment_token on success.
    """
    import os, json as _json, secrets as _sec  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.orm import Session as _OrmSession  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415
    from fastapi.responses import JSONResponse as _JR  # noqa: PLC0415
    from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415
    from xiosync.platform.ids import new_id  # noqa: PLC0415
    from xiosync.services.workers import WorkerService  # noqa: PLC0415

    expected = os.environ.get("XIOSYNC_WORKER_ORG_SECRET", "")
    if not expected or payload.worker_org_secret != expected:
        return _JR(status_code=401, media_type="application/problem+json", content={
            "type": "https://xiosync.dev/problems/unauthorized",
            "title": "Invalid worker org secret", "status": 401,
        })

    ctx = _make_ctx()
    enrollment_token = _sec.token_urlsafe(32)
    actor_id = new_id()

    with _OrmSession(get_engine()) as session:
        # Insert a worker actor row
        try:
            session.execute(text("""
                INSERT INTO actors
                  (id, organization_id, actor_type, actor_subtype, alias, state,
                   lifecycle_phase, trust_tier, health_status, created_at)
                VALUES
                  (:id, :org_id, 'worker', :subtype, :alias, 'active',
                   'operational', 'newcomer', 'healthy', now())
                ON CONFLICT DO NOTHING
            """), {
                "id": str(actor_id),
                "org_id": str(_ORG_ZERO),
                "subtype": payload.runtime_type,
                "alias": f"{payload.runtime_type}-{str(actor_id)[:8]}",
            })
            session.flush()
        except Exception as _actor_exc:
            logger.warning(
                "worker_bootstrap.actor_insert_failed",
                extra={"actor_id": str(actor_id), "error": str(_actor_exc)},
            )

        svc = WorkerService(session)
        try:
            rec = svc.register_worker(
                ctx,
                enrollment_token=enrollment_token,
                public_key=payload.public_key or f"ephemeral-{new_id()}",
                pool_type=payload.runtime_type,
                worker_id=actor_id,
                software_version=payload.software_version,
                capability_manifest=payload.reported_caps or [],
            )
        except Exception as exc:
            return _JR(status_code=422, media_type="application/problem+json", content={
                "type": "https://xiosync.dev/problems/worker_error",
                "title": "Self-enroll failed", "status": 422, "detail": str(exc),
            })

        # Approve in same session — use actor_id as approved_by (it's in actors table)
        try:
            svc.approve_worker(ctx, rec.id, approved_by=actor_id)
        except Exception:
            session.rollback()  # must rollback before continuing after a failed flush

        # Update metadata columns (re-insert actor if rollback wiped it)
        try:
            session.execute(text("""
                INSERT INTO actors
                  (id, organization_id, actor_type, actor_subtype, alias, state,
                   lifecycle_phase, trust_tier, health_status, created_at)
                VALUES
                  (:id, :org_id, 'worker', :subtype, :alias, 'active',
                   'operational', 'newcomer', 'healthy', now())
                ON CONFLICT DO NOTHING
            """), {
                "id": str(actor_id),
                "org_id": str(_ORG_ZERO),
                "subtype": payload.runtime_type,
                "alias": f"{payload.runtime_type}-{str(actor_id)[:8]}",
            })
            session.execute(text("""
                UPDATE worker_enrollments
                SET tailscale_ip = :ts_ip,
                    reported_caps = cast(:caps as jsonb),
                    runtime_type = :rtype,
                    last_seen_at = now()
                WHERE id = :eid
            """), {
                "ts_ip": payload.tailscale_ip,
                "caps": _json.dumps(payload.reported_caps or []),
                "rtype": payload.runtime_type,
                "eid": str(rec.id),
            })
        except Exception as _cap_exc:
            logger.warning(
                "worker_bootstrap.caps_update_failed",
                extra={"enrollment_id": str(rec.id), "error": str(_cap_exc)},
            )

        session.commit()
        _rec_id = rec.id

    return {
        "enrollment_id": str(_rec_id),
        "enrollment_state": "approved",
        "enrollment_token": enrollment_token,
        "note": "auto-approved",
    }


# ── TS State endpoints — Drive-backed, served via XIOSYNC ─────────────────────
# Workers fetch/save their Tailscale state through XIOSYNC.
# XIOSYNC stores it in the default storage provider (Google Drive).
# No R2 dependency after initial migration.

@router.get(
    "/workers/ts-state/{node_name}",
    summary="[Public] Download saved Tailscale state for a node (Drive-backed)",
    include_in_schema=True,
)
def get_ts_state(node_name: str):
    """Return the saved tailscaled.state for the given node from Drive storage.

    Public — no auth needed. The state file contains only a Tailscale device
    private key; it is useless without the corresponding Tailscale account.
    """
    import pathlib  # noqa: PLC0415
    from fastapi.responses import Response  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415
    from sqlalchemy.orm import Session as _OrmSession  # noqa: PLC0415
    from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415

    # Check local Drive-synced cache first
    _cache = pathlib.Path("/tmp/xio_ts_states") / f"TS_{node_name}.state"
    if _cache.exists():
        return Response(content=_cache.read_bytes(), media_type="application/json")

    # Look up the stored file path in storage_objects
    from sqlalchemy import text  # noqa: PLC0415
    with _OrmSession(get_engine()) as sess:
        row = sess.execute(text("""
            SELECT so.file_path, sp.config, sp.provider_type
            FROM storage_objects so
            JOIN storage_providers sp ON so.storage_provider_id = sp.id
            WHERE so.object_key = :key
              AND so.organization_id = :org
            ORDER BY so.created_at DESC LIMIT 1
        """), {"key": f"ts_states/TS_{node_name}.state", "org": str(_ORG_ZERO)}).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"No saved TS state for node '{node_name}'")

    # For Google Drive provider, serve via Drive file ID
    if row["provider_type"] == "google_drive":
        cfg = row["config"] if isinstance(row["config"], dict) else {}
        file_id = cfg.get("drive_file_id") or row.get("file_path", "")
        import urllib.request as _urq  # noqa: PLC0415
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        content = _urq.urlopen(url, timeout=15).read()
        return Response(content=content, media_type="application/json")

    raise HTTPException(status_code=503, detail="TS state provider not available")


@router.put(
    "/workers/ts-state/{node_name}",
    summary="[Public] Upload/update Tailscale state for a node → stored in Drive",
    include_in_schema=True,
)
async def put_ts_state(node_name: str, request: __import__("fastapi").Request):
    """Save tailscaled.state to Drive via XIOSYNC.

    Called by boot.py after Tailscale connects to persist the state.
    Uses XIOSYNC_WORKER_ORG_SECRET header for auth.
    """
    import os, pathlib  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    # Minimal auth — worker org secret in header
    secret = request.headers.get("X-Worker-Secret", "")
    expected = os.environ.get("XIOSYNC_WORKER_ORG_SECRET", "")
    if not expected or secret != expected:
        return JSONResponse(status_code=401, content={"error": "Invalid worker secret"})

    body = await request.body()
    if not body:
        return JSONResponse(status_code=400, content={"error": "Empty body"})

    # Cache locally (always — serves as a fast-path for the same-host GET)
    _cache_dir = pathlib.Path("/tmp/xio_ts_states")
    _cache_dir.mkdir(exist_ok=True)
    _local_path = _cache_dir / f"TS_{node_name}.state"
    _local_path.write_bytes(body)

    # Upload to Drive and register in storage_objects so get_ts_state can
    # find the object by key across reboots and worker nodes.
    _object_key = f"ts_states/TS_{node_name}.state"
    _drive_note = "local-only"
    try:
        import uuid as _uuid  # noqa: PLC0415
        from sqlalchemy.orm import Session as _Session  # noqa: PLC0415
        from xiosync.persistence.engine import get_engine as _get_engine  # noqa: PLC0415
        from xiosync.domain.context import OrgContext as _OrgCtx  # noqa: PLC0415
        from xiosync.subsystems.storage.service import StorageService  # noqa: PLC0415
        from sqlalchemy import text as _text  # noqa: PLC0415

        with _Session(_get_engine()) as _db:
            # Find the platform-global google_drive provider
            _prow = _db.execute(
                _text("""
                    SELECT id, config FROM storage_providers
                    WHERE provider_type = 'google_drive'
                      AND (organization_id = :org OR organization_id IS NULL)
                    ORDER BY organization_id NULLS LAST
                    LIMIT 1
                """),
                {"org": str(_ORG_ZERO)},
            ).mappings().first()

            if _prow:
                _provider_id = _prow["id"]
                _ctx = _OrgCtx(
                    organization_id=_uuid.UUID(int=0),
                    actor_id=_uuid.UUID(int=0),
                )
                _svc = StorageService(_db)

                # Try to upload via adapter (Drive FUSE or service account)
                try:
                    from xiosync.subsystems.storage.adapters.base import make_adapter  # noqa: PLC0415
                    _adapter = make_adapter(
                        provider_type="google_drive",
                        config=_prow["config"] or {},
                    )
                    if hasattr(_adapter, "put"):
                        _adapter.put(_object_key, body)
                        _drive_note = "uploaded_to_drive"
                except Exception as _adp_exc:
                    logger.warning(
                        "worker_bootstrap.ts_state.drive_upload_failed",
                        extra={"node_name": node_name, "error": str(_adp_exc)},
                    )

                # Always register/upsert the object record so get_ts_state works
                import hashlib as _hl  # noqa: PLC0415
                _svc.register_object(
                    _ctx,
                    _uuid.UUID(str(_provider_id)),
                    _object_key,
                    object_type="ts_state",
                    size_bytes=len(body),
                    checksum_sha256=_hl.sha256(body).hexdigest(),
                    content_type="application/octet-stream",
                    metadata={"node_name": node_name, "local_path": str(_local_path)},
                )
                _drive_note = _drive_note  # keep whatever the adapter set
            else:
                logger.warning(
                    "worker_bootstrap.ts_state.no_drive_provider",
                    extra={
                        "node_name": node_name,
                        "advice":    "Register a google_drive storage_provider to enable "
                                     "cross-session TS state persistence.",
                    },
                )
    except Exception as _exc:
        logger.warning(
            "worker_bootstrap.ts_state.storage_registration_failed",
            extra={"node_name": node_name, "error": str(_exc)},
        )

    return {
        "saved":     True,
        "node_name": node_name,
        "size":      len(body),
        "drive":     _drive_note,
    }
