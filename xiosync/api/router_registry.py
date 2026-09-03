"""Central router registry — routers register themselves here.

``app.py`` discovers all registered routers at startup without knowing
about specific domain routers.  Each router module calls
``register_router()`` at import time; ``app.py`` imports the routers
package (which re-imports every submodule), then calls
``get_registered_routers()`` and mounts them all in a loop.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

_registry: list[dict[str, Any]] = []


def register_router(
    router: APIRouter,
    *,
    prefix: str,
    tags: list[str],
    **kwargs: Any,
) -> None:
    """Called by each router module to self-register.

    Args:
        router:  The ``APIRouter`` instance.
        prefix:  URL prefix (e.g. ``"/browser-pools"``).
        tags:    OpenAPI tag list.
        **kwargs: Any extra kwargs forwarded to ``include_router``
                  (e.g. ``dependencies``).
    """
    _registry.append({"router": router, "prefix": prefix, "tags": tags, **kwargs})


def get_registered_routers() -> list[dict[str, Any]]:
    """Return a snapshot of all registered routers."""
    return list(_registry)
