"""Server-Sent Events (SSE) — general platform event stream.

Provides ``GET /api/v1/events/stream`` as a permanent redirect to the
canonical XIOFLOW SSE endpoint at ``/api/v1/xioflow/events/stream``.

Background
----------
The original file was a stub that polled the DB for events without connecting
to the real in-process async Queue broker.  The canonical implementation lives
in ``xiosync/subsystems/xioflow/api/events.py`` and supports:
  - Org-scoped async Queue subscriptions (no polling)
  - 30-second keepalive pings
  - ``publish_event()`` call from worker threads (thread-safe put_nowait)

Clients hitting the legacy path are redirected permanently so bookmarks and
existing client code continue to work without change.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["streaming"])


@router.get(
    "/events/stream",
    summary="Real-time SSE event stream — redirects to XIOFLOW canonical stream",
    response_class=RedirectResponse,
    include_in_schema=True,
)
async def event_stream_redirect() -> RedirectResponse:
    """Permanent redirect to the canonical XIOFLOW SSE endpoint.

    The real implementation is at ``/api/v1/xioflow/events/stream`` and
    supports org-scoped async Queue subscriptions with 30s keepalive pings.
    """
    return RedirectResponse(
        url="/api/v1/xioflow/events/stream",
        status_code=308,  # Permanent Redirect — preserves POST method
    )


from xiosync.api.router_registry import register_router  # noqa: E402
from xiosync.api.middleware.rbac import require_capability  # noqa: E402

register_router(
    router,
    prefix="/api/v1",
    tags=["streaming"],
    dependencies=[require_capability("event.manage")],
)
