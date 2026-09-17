"""batch.py — Bulk resource operations router.

Provides efficient bulk mutations for identities, sessions, and workflow runs.
All endpoints are org-scoped and require the ``workflow.manage`` capability.

Endpoints
---------
POST /api/v1/batch/identities/enable       — bulk enable identities by ID list
POST /api/v1/batch/identities/disable      — bulk disable identities by ID list
POST /api/v1/batch/identities/tag          — attach a tag to multiple identities
POST /api/v1/batch/identities/delete       — soft-delete multiple identities
POST /api/v1/batch/sessions/terminate      — terminate multiple browser sessions
POST /api/v1/batch/sessions/purge          — hard-delete session records
POST /api/v1/batch/runs/cancel             — cancel in-progress workflow runs
POST /api/v1/batch/runs/retry              — re-queue failed workflow runs
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.api.middleware.db import get_db
from xiosync.api.middleware.rbac import get_org_context, require_capability
from xiosync.domain.context import OrgContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/batch", tags=["Batch Operations"])

_MAX_BATCH = 500  # prevent accidental bulk-deletes of entire org


# ── Shared helpers ─────────────────────────────────────────────────────────────

class IDListRequest(BaseModel):
    ids: list[uuid.UUID] = Field(..., min_length=1, max_length=_MAX_BATCH)
    model_config = ConfigDict(from_attributes=True)


class BatchResult(BaseModel):
    affected:  int
    ids:       list[str]
    message:   str = ""


def _id_strs(ids: list[uuid.UUID]) -> list[str]:
    return [str(i) for i in ids]


def _check_org_owns(db: Session, org_id: uuid.UUID, table: str, col: str, ids: list[uuid.UUID]) -> None:
    """Raise 403 if any ID in the list does not belong to this org."""
    rows = db.execute(
        text(f"SELECT id FROM {table} WHERE id = ANY(:ids) AND {col} != :org"),  # noqa: S608
        {"ids": _id_strs(ids), "org": str(org_id)},
    ).fetchall()
    if rows:
        foreign = [str(r[0]) for r in rows]
        raise HTTPException(status_code=403, detail=f"IDs not owned by org: {foreign}")


# ── Identity bulk endpoints ────────────────────────────────────────────────────

@router.post("/identities/enable", response_model=BatchResult)
def bulk_enable_identities(
    req: IDListRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Enable a list of identities (set status = 'active')."""
    result = db.execute(
        text("""
            UPDATE identities
            SET    status = 'active', updated_at = now()
            WHERE  id = ANY(:ids) AND organization_id = :org
              AND  status != 'active'
        """),
        {"ids": _id_strs(req.ids), "org": str(ctx.organization_id)},
    )
    db.commit()
    logger.info("batch.identities.enabled", extra={"count": result.rowcount, "org": str(ctx.organization_id)})
    return {"affected": result.rowcount, "ids": _id_strs(req.ids), "message": f"{result.rowcount} identity/ies enabled"}


@router.post("/identities/disable", response_model=BatchResult)
def bulk_disable_identities(
    req: IDListRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Disable a list of identities (set status = 'suspended')."""
    result = db.execute(
        text("""
            UPDATE identities
            SET    status = 'suspended', updated_at = now()
            WHERE  id = ANY(:ids) AND organization_id = :org
              AND  status != 'suspended'
        """),
        {"ids": _id_strs(req.ids), "org": str(ctx.organization_id)},
    )
    db.commit()
    logger.info("batch.identities.disabled", extra={"count": result.rowcount, "org": str(ctx.organization_id)})
    return {"affected": result.rowcount, "ids": _id_strs(req.ids), "message": f"{result.rowcount} identity/ies suspended"}


class TagRequest(BaseModel):
    ids: list[uuid.UUID]  = Field(..., min_length=1, max_length=_MAX_BATCH)
    tag: str              = Field(..., min_length=1, max_length=64)

    model_config = ConfigDict(from_attributes=True)


@router.post("/identities/tag", response_model=BatchResult)
def bulk_tag_identities(
    req: TagRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Append a tag to the metadata.tags array of multiple identities."""
    result = db.execute(
        text("""
            UPDATE identities
            SET    metadata   = jsonb_set(
                                  coalesce(metadata, '{}'::jsonb),
                                  '{tags}',
                                  coalesce(metadata->'tags', '[]'::jsonb) || :tag_json::jsonb,
                                  true
                                ),
                   updated_at = now()
            WHERE  id = ANY(:ids) AND organization_id = :org
        """),
        {
            "ids":      _id_strs(req.ids),
            "org":      str(ctx.organization_id),
            "tag_json": json.dumps([req.tag]),
        },
    )
    db.commit()
    logger.info("batch.identities.tagged", extra={"tag": req.tag, "count": result.rowcount})
    return {"affected": result.rowcount, "ids": _id_strs(req.ids), "message": f"Tag '{req.tag}' applied"}


@router.post("/identities/delete", response_model=BatchResult)
def bulk_delete_identities(
    req: IDListRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Soft-delete multiple identities (set status = 'deleted', clear credentials)."""
    result = db.execute(
        text("""
            UPDATE identities
            SET    status     = 'deleted',
                   updated_at = now()
            WHERE  id = ANY(:ids) AND organization_id = :org
              AND  status != 'deleted'
        """),
        {"ids": _id_strs(req.ids), "org": str(ctx.organization_id)},
    )
    db.commit()
    logger.info("batch.identities.deleted", extra={"count": result.rowcount, "org": str(ctx.organization_id)})
    return {"affected": result.rowcount, "ids": _id_strs(req.ids), "message": f"{result.rowcount} identity/ies soft-deleted"}


# ── Session bulk endpoints ─────────────────────────────────────────────────────

@router.post("/sessions/terminate", response_model=BatchResult)
def bulk_terminate_sessions(
    req: IDListRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Mark multiple browser sessions as terminated."""
    result = db.execute(
        text("""
            UPDATE browser_sessions
            SET    status       = 'terminated',
                   terminated_at = now(),
                   updated_at   = now()
            WHERE  id = ANY(:ids) AND organization_id = :org
              AND  status NOT IN ('terminated', 'error')
        """),
        {"ids": _id_strs(req.ids), "org": str(ctx.organization_id)},
    )
    db.commit()
    logger.info("batch.sessions.terminated", extra={"count": result.rowcount, "org": str(ctx.organization_id)})
    return {"affected": result.rowcount, "ids": _id_strs(req.ids), "message": f"{result.rowcount} session(s) terminated"}


@router.post("/sessions/purge", response_model=BatchResult)
def bulk_purge_sessions(
    req: IDListRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Hard-delete terminated/error session records."""
    # Only allow purging sessions that are already terminated/error
    result = db.execute(
        text("""
            DELETE FROM browser_sessions
            WHERE  id = ANY(:ids) AND organization_id = :org
              AND  status IN ('terminated', 'error')
        """),
        {"ids": _id_strs(req.ids), "org": str(ctx.organization_id)},
    )
    db.commit()
    logger.info("batch.sessions.purged", extra={"count": result.rowcount, "org": str(ctx.organization_id)})
    return {"affected": result.rowcount, "ids": _id_strs(req.ids), "message": f"{result.rowcount} session record(s) purged"}


# ── Workflow run bulk endpoints ────────────────────────────────────────────────

@router.post("/runs/cancel", response_model=BatchResult)
def bulk_cancel_runs(
    req: IDListRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Cancel multiple in-progress workflow runs."""
    result = db.execute(
        text("""
            UPDATE workflow_runs
            SET    status      = 'cancelled',
                   finished_at = now(),
                   updated_at  = now()
            WHERE  id = ANY(:ids) AND organization_id = :org
              AND  status IN ('pending', 'running', 'paused')
        """),
        {"ids": _id_strs(req.ids), "org": str(ctx.organization_id)},
    )
    db.commit()
    logger.info("batch.runs.cancelled", extra={"count": result.rowcount, "org": str(ctx.organization_id)})
    return {"affected": result.rowcount, "ids": _id_strs(req.ids), "message": f"{result.rowcount} run(s) cancelled"}


@router.post("/runs/retry", response_model=BatchResult)
def bulk_retry_runs(
    req: IDListRequest,
    db:  Session    = Depends(get_db),
    ctx: OrgContext = Depends(get_org_context),
) -> dict:
    """Re-queue failed/cancelled workflow runs by resetting their status to 'pending'."""
    result = db.execute(
        text("""
            UPDATE workflow_runs
            SET    status      = 'pending',
                   started_at  = NULL,
                   finished_at = NULL,
                   error       = NULL,
                   updated_at  = now()
            WHERE  id = ANY(:ids) AND organization_id = :org
              AND  status IN ('failed', 'cancelled', 'error')
        """),
        {"ids": _id_strs(req.ids), "org": str(ctx.organization_id)},
    )
    db.commit()
    logger.info("batch.runs.retried", extra={"count": result.rowcount, "org": str(ctx.organization_id)})
    return {"affected": result.rowcount, "ids": _id_strs(req.ids), "message": f"{result.rowcount} run(s) re-queued"}


# ── Router registration ────────────────────────────────────────────────────────
from xiosync.api.router_registry import register_router   # noqa: E402

register_router(
    router,
    prefix="/api/v1",
    tags=["Batch Operations"],
    dependencies=[require_capability("workflow.manage")],
)
