"""worker_locks.py — Distributed lock API for Colab workers.

Backed by XIOSYNC's Redis instance. Workers call these endpoints to
coordinate concurrent access to shared Drive FUSE filesystem objects.

Endpoints (public — auth via X-Worker-Secret header):
  POST /workers/lock/acquire   — atomic Redis SETNX + TTL
  POST /workers/lock/release   — DEL only if current holder matches
  GET  /workers/lock/status/{key} — inspect lock state

Redis key pattern:  xio:objlock:{sha256_of_resource_key}

Why Redis?
  - SETNX is truly atomic across all workers (no race condition).
  - TTL auto-expires dead worker locks (no manual cleanup needed).
  - XIOSYNC server is reachable by all workers via Tailscale.
  - Filesystem flock() does NOT propagate across different VMs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

from fastapi import APIRouter, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["worker-locks"])

# ── Redis setup ───────────────────────────────────────────────────────────────

_REDIS_KEY_PREFIX = "xio:objlock:"
_DEFAULT_TTL_S    = 60
_MAX_TTL_S        = 300


def _get_redis():
    """Return a Redis client using REDIS_URL env var."""
    import redis as _redis  # noqa: PLC0415
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return _redis.Redis.from_url(url, decode_responses=True, socket_timeout=3)


def _redis_key(resource_key: str) -> str:
    """Stable Redis key for a resource_key."""
    # Hash the resource key so it's safe for Redis key limits and avoids injection
    safe = hashlib.sha256(resource_key.encode()).hexdigest()[:32]
    return f"{_REDIS_KEY_PREFIX}{safe}"


def _worker_secret_ok(x_worker_secret: str | None) -> bool:
    expected = os.environ.get("XIOSYNC_WORKER_ORG_SECRET", "")
    return bool(expected) and x_worker_secret == expected


# ── Pydantic models ───────────────────────────────────────────────────────────

class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AcquireRequest(_Base):
    resource_key: str = Field(
        description="Logical resource key (e.g. 'ts_states/TS_colab-master.state')",
        max_length=512,
    )
    node_name: str = Field(
        description="Unique name of the requesting worker node",
        max_length=128,
    )
    ttl_seconds: int = Field(
        default=_DEFAULT_TTL_S,
        ge=1,
        le=_MAX_TTL_S,
        description="Seconds before the lock auto-expires (dead node protection)",
    )


class AcquireResponse(_Base):
    model_config = ConfigDict(extra="ignore")
    acquired: bool
    holder:   str | None = None   # node that currently holds the lock (if not acquired)
    ttl_seconds: int | None = None


class ReleaseRequest(_Base):
    resource_key: str = Field(max_length=512)
    node_name: str    = Field(max_length=128)


class ReleaseResponse(_Base):
    model_config = ConfigDict(extra="ignore")
    released: bool
    reason: str = ""


class LockStatusResponse(_Base):
    model_config = ConfigDict(extra="ignore")
    locked: bool
    holder: str | None = None
    acquired_at: float | None = None
    ttl_seconds: int | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/workers/lock/acquire",
    response_model=AcquireResponse,
    summary="[Worker] Acquire a distributed lock (Redis SETNX)",
)
def acquire_lock(
    payload: AcquireRequest,
    x_worker_secret: str | None = Header(default=None),
) -> AcquireResponse:
    """Attempt to acquire a distributed lock for a shared Drive resource.

    Uses Redis SET ... NX EX (atomic set-if-not-exists with TTL).

    Returns {acquired: true} if the lock was taken.
    Returns {acquired: false, holder: "node-name"} if already held.
    """
    if not _worker_secret_ok(x_worker_secret):
        raise HTTPException(status_code=401, detail="Invalid X-Worker-Secret")

    try:
        r = _get_redis()
        rkey = _redis_key(payload.resource_key)
        value = json.dumps({
            "node":        payload.node_name,
            "acquired_at": time.time(),
            "resource":    payload.resource_key,
        })

        # SET key value NX EX ttl  (atomic: only sets if key doesn't exist)
        acquired = r.set(rkey, value, nx=True, ex=payload.ttl_seconds)

        if acquired:
            logger.debug(
                "lock.acquired resource=%r node=%r ttl=%d",
                payload.resource_key, payload.node_name, payload.ttl_seconds,
            )
            return AcquireResponse(
                acquired=True,
                ttl_seconds=payload.ttl_seconds,
            )
        else:
            # Read current holder for debugging
            raw = r.get(rkey)
            holder = None
            ttl_remaining = r.ttl(rkey)
            if raw:
                try:
                    holder = json.loads(raw).get("node")
                except Exception:
                    holder = str(raw)[:64]
            logger.debug(
                "lock.denied resource=%r requested_by=%r holder=%r",
                payload.resource_key, payload.node_name, holder,
            )
            return AcquireResponse(
                acquired=False,
                holder=holder,
                ttl_seconds=max(ttl_remaining, 0) if ttl_remaining else None,
            )

    except Exception as exc:
        logger.exception("lock.acquire_error resource=%r", payload.resource_key)
        raise HTTPException(status_code=503, detail=f"Lock service error: {exc}") from exc


@router.post(
    "/workers/lock/release",
    response_model=ReleaseResponse,
    summary="[Worker] Release a distributed lock",
)
def release_lock(
    payload: ReleaseRequest,
    x_worker_secret: str | None = Header(default=None),
) -> ReleaseResponse:
    """Release a lock — only if the requesting node is the current holder.

    Prevents a crashed-then-restarted node from releasing a lock it no
    longer holds (which would evict a legitimate holder).
    """
    if not _worker_secret_ok(x_worker_secret):
        raise HTTPException(status_code=401, detail="Invalid X-Worker-Secret")

    try:
        r = _get_redis()
        rkey = _redis_key(payload.resource_key)
        raw = r.get(rkey)

        if raw is None:
            return ReleaseResponse(released=False, reason="lock_not_found")

        try:
            holder = json.loads(raw).get("node")
        except Exception:
            holder = None

        if holder != payload.node_name:
            return ReleaseResponse(
                released=False,
                reason=f"not_holder (held by {holder!r})",
            )

        r.delete(rkey)
        logger.debug("lock.released resource=%r node=%r", payload.resource_key, payload.node_name)
        return ReleaseResponse(released=True)

    except Exception as exc:
        logger.exception("lock.release_error resource=%r", payload.resource_key)
        raise HTTPException(status_code=503, detail=f"Lock service error: {exc}") from exc


@router.get(
    "/workers/lock/status/{resource_key:path}",
    response_model=LockStatusResponse,
    summary="[Worker] Inspect lock state for a resource key",
)
def lock_status(
    resource_key: str = Path(description="URL-encoded resource key"),
    x_worker_secret: str | None = Header(default=None),
) -> LockStatusResponse:
    """Check whether a resource key is currently locked and by whom."""
    if not _worker_secret_ok(x_worker_secret):
        raise HTTPException(status_code=401, detail="Invalid X-Worker-Secret")

    try:
        r = _get_redis()
        rkey = _redis_key(resource_key)
        raw = r.get(rkey)

        if raw is None:
            return LockStatusResponse(locked=False)

        ttl = r.ttl(rkey)
        try:
            data = json.loads(raw)
        except Exception:
            data = {}

        return LockStatusResponse(
            locked=True,
            holder=data.get("node"),
            acquired_at=data.get("acquired_at"),
            ttl_seconds=max(ttl, 0) if ttl else None,
        )

    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Lock service error: {exc}") from exc
