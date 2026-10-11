"""FastAPI app factory and assembly for xiorun-agent."""

from __future__ import annotations

import logging

from fastapi import FastAPI

try:
    from colab.agent.server.lifespan import _lifespan
except ImportError:
    try:
        from agent.server.lifespan import _lifespan
    except ImportError:
        from contextlib import asynccontextmanager as _acm

        @_acm
        async def _lifespan(app):
            yield

logger = logging.getLogger("xiorun_agent")

app = FastAPI(
    title="xiorun-agent",
    version="1.0.0",
    lifespan=_lifespan,
)

# Import and include modular routers
try:
    from colab.agent.display.cdp_proxy import router as cdp_router
    app.include_router(cdp_router)
except ImportError:
    try:
        from agent.display.cdp_proxy import router as cdp_router
        app.include_router(cdp_router)
    except ImportError:
        logger.warning("app: cdp_proxy router not available")

try:
    from colab.agent.ai.ai_routes import router as ai_router
    app.include_router(ai_router)
except ImportError:
    try:
        from agent.ai.ai_routes import router as ai_router
        app.include_router(ai_router)
    except ImportError:
        logger.warning("app: ai_routes router not available")


def create_app() -> FastAPI:
    """Return the configured xiorun-agent application."""
    return app
