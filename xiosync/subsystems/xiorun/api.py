"""xiorun router — internal callbacks + session management API.

Routes:
  Internal (Colab → XIOSYNC, no RBAC — validated by shared secret):
    POST /internal/xiorun/proxy-lost      — Colab agent notifies proxy loss
    POST /internal/xiorun/browser-crashed — Colab agent notifies Chromium crash

  Session management (UI/API consumers, requires session.observe capability):
    GET  /xiorun/sessions                 — list active browser sessions + pool stats
    GET  /xiorun/sessions/{session_id}    — single session detail + CDP URL
    POST /xiorun/sessions/{session_id}/terminate — graceful teardown
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["xiorun"])

# ── Shared secret for Colab → XIOSYNC internal calls ─────────────────────────
# The Colab boot.py sets XIORUN_XIOSYNC_TOKEN from XIOSYNC_INTERNAL_SECRET.
# XIOSYNC_INTERNAL_SECRET must be set in the XIOSYNC server environment.
_INTERNAL_SECRET = os.environ.get("XIOSYNC_INTERNAL_SECRET", "")


def _check_internal_secret(authorization: str | None) -> None:
    """Validate Bearer token for internal Colab → XIOSYNC calls."""
    if not _INTERNAL_SECRET:
        raise HTTPException(
            status_code=503,
            detail="XIOSYNC_INTERNAL_SECRET not configured on server",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ")
    if token != _INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Invalid internal token")


# ── Pydantic models ────────────────────────────────────────────────────────────

class ProxyLostPayload(BaseModel):
    session_id: str
    node: str = ""


class BrowserCrashedPayload(BaseModel):
    session_id: str
    node: str = ""
    reason: str = "unknown"


# ── Internal endpoints (Colab → XIOSYNC) ──────────────────────────────────────

@router.post(
    "/internal/xiorun/proxy-lost",
    summary="Colab agent notifies XIOSYNC that exit-node proxy is lost",
    include_in_schema=False,   # hide from public OpenAPI docs
)
def proxy_lost(
    payload: ProxyLostPayload,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Called by xiorun_agent.py when SOCKS5 proxy probe fails 2 consecutive times.

    This is the Colab-side enforcement of the no-datacenter-IP policy.
    Chromium is already killed on the Colab node before this is called.
    XIOSYNC marks the session failed and cancels the running DAG task.
    """
    _check_internal_secret(authorization)

    session_id = payload.session_id
    logger.error("xiorun.api.proxy_lost", extra={
        "session_id": session_id,
        "node":       payload.node,
    })

    # Mark session failed in DB
    try:
        from sqlalchemy import text  # noqa: PLC0415
        session = request.state.org_session
        session.execute(
            text("""
                UPDATE browser_sessions
                SET state = 'failed', updated_at = now()
                WHERE id = :sid
            """),
            {"sid": session_id},
        )
        session.commit()
    except Exception as exc:
        logger.warning("xiorun.api.proxy_lost.db_error", extra={"error": str(exc)})

    # Cancel any running DAG task via runtime_pool (best-effort)
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
        pool = get_runtime_pool()
        launcher = pool.get_launcher(session_id)
        if launcher:
            import asyncio  # noqa: PLC0415
            loop = asyncio.get_event_loop()
            if loop.is_running() and launcher._run_task and not launcher._run_task.done():
                launcher._run_task.cancel()
            pool._unregister(session_id)
    except Exception as exc:
        logger.warning("xiorun.api.proxy_lost.pool_error", extra={"error": str(exc)})

    return {"ok": True, "session_id": session_id, "state": "failed"}


@router.post(
    "/internal/xiorun/browser-crashed",
    summary="Colab agent notifies XIOSYNC that Chromium crashed",
    include_in_schema=False,
)
def browser_crashed(
    payload: BrowserCrashedPayload,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Called by xiorun_agent.py when the Chromium process dies unexpectedly."""
    _check_internal_secret(authorization)

    session_id = payload.session_id
    logger.error("xiorun.api.browser_crashed", extra={
        "session_id": session_id,
        "node":       payload.node,
        "reason":     payload.reason,
    })

    try:
        from sqlalchemy import text  # noqa: PLC0415
        session = request.state.org_session
        session.execute(
            text("""
                UPDATE browser_sessions
                SET state = 'failed', updated_at = now()
                WHERE id = :sid
            """),
            {"sid": session_id},
        )
        session.commit()
    except Exception as exc:
        logger.warning("xiorun.api.browser_crashed.db_error", extra={"error": str(exc)})

    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415
        get_runtime_pool()._unregister(session_id)
    except Exception:
        pass

    return {"ok": True, "session_id": session_id, "state": "failed"}


# ── Session management endpoints ───────────────────────────────────────────────

@router.get(
    "/xiorun/sessions",
    summary="List active XIORUN browser sessions with pool stats",
)
def list_xiorun_sessions(request: Request) -> dict[str, Any]:
    """Returns all browser sessions currently active or initializing.

    Merges DB state with live runtime_pool data (CDP URL, page closed status).
    Suitable for XIOVIEW session picker and pool utilization dashboards.
    """
    from sqlalchemy import text  # noqa: PLC0415
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

    session = request.state.org_session
    org_ctx  = request.state.org_context

    rows = session.execute(
        text("""
            SELECT
                bs.id::text             AS session_id,
                bs.state,
                bs.worker_ts_ip,
                bs.proxy_url,
                bs.pool_id::text        AS pool_id,
                bp.name                 AS pool_name,
                bp.max_instances,
                bs.pppoe_exit_node_id::text AS exit_node_id,
                bs.public_ip,
                bs.created_at,
                bs.updated_at
            FROM browser_sessions bs
            JOIN browser_pools bp ON bp.id = bs.pool_id
            WHERE bs.organization_id = :org
              AND bs.state IN ('initializing', 'active', 'suspended')
            ORDER BY bs.created_at DESC
            LIMIT 100
        """),
        {"org": str(org_ctx.organization_id)},
    ).mappings().all()

    pool = get_runtime_pool()
    live = {e["session_id"]: e for e in pool.list_sessions()}

    sessions = []
    for row in rows:
        sid   = row["session_id"]
        entry = live.get(sid)
        sessions.append({
            "session_id":    sid,
            "state":         row["state"],
            "worker_ts_ip":  row["worker_ts_ip"],
            "proxy_url":     row["proxy_url"],
            "pool_id":       row["pool_id"],
            "pool_name":     row["pool_name"],
            "max_instances": row["max_instances"],
            "exit_node_id":  row["exit_node_id"],
            "public_ip":     row["public_ip"],
            "created_at":    row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at":    row["updated_at"].isoformat() if row["updated_at"] else None,
            # Live runtime data (None if not in pool — session is suspended or pool restarted)
            "cdp_live":      entry is not None,
            "page_closed":   entry["page_closed"] if entry else None,
        })

    # Pool utilization summary
    pool_stats: dict[str, dict] = {}
    for s in sessions:
        pid = s["pool_id"]
        if pid not in pool_stats:
            pool_stats[pid] = {
                "pool_id":       pid,
                "pool_name":     s["pool_name"],
                "max_instances": s["max_instances"],
                "active":        0,
                "initializing":  0,
                "suspended":     0,
            }
        pool_stats[pid][s["state"]] = pool_stats[pid].get(s["state"], 0) + 1

    return {
        "sessions":   sessions,
        "total":      len(sessions),
        "live_count": sum(1 for s in sessions if s["cdp_live"]),
        "pool_stats": list(pool_stats.values()),
    }


@router.get(
    "/xiorun/sessions/{session_id}",
    summary="Get XIORUN session detail",
)
def get_xiorun_session(session_id: str, request: Request) -> dict[str, Any]:
    """Returns full detail for one session including live CDP WebSocket URL if active."""
    from sqlalchemy import text  # noqa: PLC0415
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

    session = request.state.org_session
    org_ctx  = request.state.org_context

    row = session.execute(
        text("""
            SELECT
                bs.id::text             AS session_id,
                bs.state,
                bs.worker_ts_ip,
                bs.proxy_url,
                bs.pool_id::text        AS pool_id,
                bp.name                 AS pool_name,
                bp.max_instances,
                bp.engine_type,
                bp.stealth_config,
                bs.pppoe_exit_node_id::text AS exit_node_id,
                bs.public_ip,
                bs.session_data,
                bs.created_at,
                bs.updated_at
            FROM browser_sessions bs
            JOIN browser_pools bp ON bp.id = bs.pool_id
            WHERE bs.id = :sid
              AND bs.organization_id = :org
        """),
        {"sid": session_id, "org": str(org_ctx.organization_id)},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    pool    = get_runtime_pool()
    page    = pool.get_page(session_id)
    launcher = pool.get_launcher(session_id)

    # Derive CDP WS URL from worker_ts_ip if launcher is alive
    cdp_ws_url: str | None = None
    if launcher and hasattr(launcher, "_browser") and launcher._browser:
        try:
            cdp_ws_url = launcher._browser.ws_endpoint
        except Exception:
            pass

    return {
        "session_id":   row["session_id"],
        "state":        row["state"],
        "worker_ts_ip": row["worker_ts_ip"],
        "proxy_url":    row["proxy_url"],
        "pool_id":      row["pool_id"],
        "pool_name":    row["pool_name"],
        "max_instances": row["max_instances"],
        "engine_type":  row["engine_type"],
        "stealth_config": dict(row["stealth_config"] or {}),
        "exit_node_id": row["exit_node_id"],
        "public_ip":    row["public_ip"],
        "session_data": dict(row["session_data"] or {}),
        "created_at":   row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at":   row["updated_at"].isoformat() if row["updated_at"] else None,
        "cdp_live":     page is not None,
        "cdp_ws_url":   cdp_ws_url,
    }


@router.post(
    "/xiorun/sessions/{session_id}/terminate",
    summary="Gracefully terminate an XIORUN browser session",
)
async def terminate_xiorun_session(session_id: str, request: Request) -> dict[str, Any]:
    """Triggers graceful teardown: profile save → Chromium terminate → state=suspended."""
    from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

    org_ctx  = request.state.org_context
    pool     = get_runtime_pool()
    launcher = pool.get_launcher(session_id)

    if launcher is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session {session_id} not live in runtime pool. Use DB state management for offline sessions.",
        )

    await pool.release(
        session_id,
        str(org_ctx.organization_id),
        page_url=None,
    )

    return {"ok": True, "session_id": session_id, "state": "suspended"}
