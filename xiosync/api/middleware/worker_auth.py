"""Unified worker authentication dependency (Phase 2 audit — Fix 6).

Consolidates the two worker auth mechanisms (X-Worker-Secret and
X-XIOSYNC-Internal) into a single reusable FastAPI dependency.

Both headers are accepted for backward compatibility. The dependency
validates against both XIOSYNC_WORKER_ORG_SECRET and XIOSYNC_INTERNAL_SECRET
environment variables.

Usage in a router::

    from xiosync.api.middleware.worker_auth import verify_worker_auth

    @router.get("/some-internal-endpoint")
    def my_endpoint(
        request: Request,
        _auth: None = Depends(verify_worker_auth),
    ) -> dict:
        ...

Or as a router-level dependency::

    internal_router = APIRouter(
        prefix="/xioflow/events",
        dependencies=[Depends(verify_worker_auth)],
    )
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import HTTPException, Request


def verify_worker_auth(request: Request) -> None:
    """Validate worker identity via either auth header.

    Checks (in order):
      1. ``X-XIOSYNC-Internal`` against ``XIOSYNC_INTERNAL_SECRET``
      2. ``X-Worker-Secret`` against ``XIOSYNC_WORKER_ORG_SECRET``

    Raises ``HTTPException(401)`` if neither header matches.
    """
    internal_secret = os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    worker_secret = os.environ.get("XIOSYNC_WORKER_ORG_SECRET", "")

    given_internal = request.headers.get("X-XIOSYNC-Internal", "")
    given_worker = request.headers.get("X-Worker-Secret", "")

    # Accept either valid header
    if internal_secret and given_internal == internal_secret:
        return
    if worker_secret and given_worker == worker_secret:
        return

    raise HTTPException(
        status_code=401,
        detail="Missing or invalid worker authentication. "
               "Provide X-XIOSYNC-Internal or X-Worker-Secret header.",
    )
