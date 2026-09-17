"""FastAPI composition root for the XIOSYNC control plane.

Normative references:
- Phase 7 Step 1: Application Lifecycle & Readiness Head-gates
  - M5: Fail-fast startup with strict config validation
  - M7: Distinct /live and /ready endpoints
  - C6: Migration-as-deploy-step with readiness head-gate
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from starlette.types import Receive, Scope, Send

from xiosync.api.middleware import (
    DEFAULT_MAX_BODY_BYTES,
    AuthenticationMiddleware,
    BodySizeLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)
from xiosync.api.middleware.cors import StrictCORSMiddleware
from xiosync.api.middleware.rate_limit import RateLimitMiddleware
from xiosync.api.middleware.versioning import VersionGovernanceMiddleware
from xiosync.api.routers.auth import router as auth_router
from xiosync.api.routers.batch import router as batch_router
from xiosync.api.routers.dlq import router as dlq_router
from xiosync.api.routers.execution import router as execution_router
from xiosync.api.routers.health import router as health_router
from xiosync.api.routers.listings import router as listings_router
from xiosync.api.routers.plugins import router as plugins_router
from xiosync.api.routers.streaming import router as streaming_router
from xiosync.api.routers.task_streams import router as task_streams_router
from xiosync.api.routers.triggers import router as triggers_router
from xiosync.core.health import verify_migrations_at_head
from xiosync.core.rate_limit import (
    RateLimitConfig,
    RateLimiter,
    create_rate_limiter,
    get_rate_limit_config,
)
from xiosync.persistence.database import create_database_engine
from xiosync.persistence.identity import IdentityRepository
from xiosync.platform.clock import Clock, SystemClock
from xiosync.platform.config import ConfigError, load_config
from xiosync.platform.telemetry import configure_logging
from xiosync.services.identity import SessionService

logger = logging.getLogger(__name__)


def create_app(
    *,
    session_service: SessionService,
    engine: Engine,
    clock: Clock,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    rate_limiter: RateLimiter | None = None,
    rate_limit_config: Callable[[str], RateLimitConfig] | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Compose an app from explicit dependencies (the contract-test seam).

    Health check endpoints are registered at the root path (not under /api/v1)
    so orchestrators can easily probe them without authentication.
    """
    # P3: Lifespan handler for graceful startup/shutdown.
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator
    import logging as _logging

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        _log = _logging.getLogger("xiosync.api")
        _log.info("startup: XIOSYNC API starting")
        # Register engine singleton so subsystems can reach DB outside request context
        from xiosync.platform.engine_ref import set_engine as _set_engine
        _set_engine(engine)
        yield
        # Graceful shutdown: dispose connection pool and close Redis.
        _log.info("shutdown: disposing database connection pool")
        engine.dispose()
        if rate_limiter is not None:
            try:
                from xiosync.core.rate_limit import close_rate_limiter
                close_rate_limiter(rate_limiter)
                _log.info("shutdown: Redis rate limiter closed")
            except Exception:
                pass
        _log.info("shutdown: XIOSYNC API stopped")

    application = FastAPI(title="XIOSYNC API", version="1.0.0", lifespan=_lifespan)
    application.state.session_service = session_service
    application.state.engine = engine
    application.state.clock = clock
    application.state.rate_limiter = rate_limiter  # For per-capability rate checking

    # P11: Observability — request metrics middleware + optional /metrics.
    from xiosync.platform.observability import (
        ObservabilityMiddleware,
        get_metrics_app,
        setup_opentelemetry,
    )
    application.add_middleware(ObservabilityMiddleware)
    setup_opentelemetry()
    metrics_app = get_metrics_app()
    if metrics_app is not None:
        application.mount("/metrics", metrics_app)

    # Health check endpoints (no auth required, no /api/v1 prefix)
    application.include_router(health_router)

    # --- Genesis Phase 1: RBAC enforcement (Gap G-3) -------------------------
    # Each router gets a capability group dependency that checks the actor's
    # membership role before any route handler executes.  The capability groups
    # are configurable per-org (Q2 Option C).
    from xiosync.api.middleware.rbac import require_capability

    # Auth is public — no RBAC (already handled by its own logic)
    application.include_router(auth_router, prefix="/api/v1")

    # Task execution — script / http / node on-demand execution
    application.include_router(
        execution_router, prefix="/api/v1",
        dependencies=[require_capability("task.execute")],
    )

    # Dead letter queue — wired
    application.include_router(
        dlq_router, prefix='/api/v1',
        dependencies=[require_capability("dlq.manage")],
    )

    # Plugins — requires plugin.admin capability
    application.include_router(
        plugins_router, prefix="/api/v1",
        dependencies=[require_capability("plugin.admin")],
    )

    # Listings — read-only, requires readonly capability
    application.include_router(
        listings_router, prefix="/api/v1",
        dependencies=[require_capability("readonly")],
    )

    # Batch operations — bulk mutations on identities, sessions, runs
    application.include_router(
        batch_router, prefix="/api/v1",
        dependencies=[require_capability("workflow.manage")],
    )

    # SSE streaming — requires event.manage capability
    application.include_router(
        streaming_router, prefix="/api/v1",
        dependencies=[require_capability("event.manage")],
    )

    # Triggers — requires trigger.manage capability
    application.include_router(
        triggers_router, prefix="/api/v1",
        dependencies=[require_capability("trigger.manage")],
    )

    # Task output streams — requires task.execute capability
    application.include_router(
        task_streams_router, prefix="/api/v1",
        dependencies=[require_capability("task.execute")],
    )

    # Metering — read-only
    from xiosync.api.routers.metering import router as metering_router
    application.include_router(
        metering_router, prefix="/api/v1",
        dependencies=[require_capability("metering.read")],
    )

    # Actors — requires actor.manage capability
    from xiosync.api.routers.actors import router as actors_router
    application.include_router(
        actors_router, prefix="/api/v1",
        dependencies=[require_capability("actor.manage")],
    )

    # Organizations — bootstrap is public (handled within the route),
    # current org info requires readonly
    from xiosync.api.routers.organizations import router as organizations_router
    from xiosync.api.routers.projects import router as projects_router
    # Projects router carries per-route RBAC (project.read / project.manage).
    application.include_router(projects_router, prefix="/api/v1")
    application.include_router(
        organizations_router, prefix="/api/v1",
    )

    # --- XIOFLOW: workflow template library (workflow.manage capability)
    from xiosync.subsystems.xioflow.api.templates import router as xioflow_templates_router
    application.include_router(
        xioflow_templates_router, prefix="/api/v1",
        dependencies=[require_capability("workflow.manage")],
    )

    # --- XIOFLOW: run/DLQ/events management
    from xiosync.subsystems.xioflow.api.events import router as xioflow_events_router
    application.include_router(
        xioflow_events_router, prefix="/api/v1",
        dependencies=[require_capability("workflow.manage")],
    )

    # --- XIOFLOW: memory node recording + graph (teacher extension + operators)
    from xiosync.subsystems.xioflow.api.memory import router as xioflow_memory_router
    application.include_router(
        xioflow_memory_router, prefix="/api/v1",
        dependencies=[require_capability("workflow.manage")],
    )

    # --- XIOFLOW: compute node registry (plugin sandbox)
    from xiosync.subsystems.xioflow.api.compute_nodes import router as xioflow_compute_router
    application.include_router(
        xioflow_compute_router, prefix="/api/v1",
        dependencies=[require_capability("workflow.manage")],
    )

    # --- XIOFLOW: run dispatch + lifecycle + HITL pause/resume + stats
    from xiosync.subsystems.xioflow.api.runs import router as xioflow_runs_router
    application.include_router(
        xioflow_runs_router, prefix="/api/v1",
        dependencies=[require_capability("workflow.manage")],
    )

    # --- XIOFLOW: DAG deploy + script→DAG convert (Gemini structured output)
    from xiosync.subsystems.xioflow.api.dag import router as xioflow_dag_router
    application.include_router(
        xioflow_dag_router, prefix="/api/v1",
        dependencies=[require_capability("workflow.manage")],
    )

    # --- XIOFLOW: trigger management (cron + event triggers CRUD)
    from xiosync.subsystems.xioflow.api.triggers import router as xioflow_triggers_router
    application.include_router(
        xioflow_triggers_router, prefix="/api/v1",
        dependencies=[require_capability("workflow.manage")],
    )

    # --- XIOVIEW: universal browser session observation (WS stream + remote control)
    from xiosync.subsystems.xioview.api.observe import (  # noqa: PLC0415
        router as xioview_router,
        public_router as xioview_public_router,
    )
    # Public endpoints — attach (uses internal-secret) + viewer HTML (no auth)
    application.include_router(xioview_public_router, prefix="/api/v1")
    # Protected endpoints — observe WS, fps control, session list
    application.include_router(
        xioview_router, prefix="/api/v1",
        dependencies=[require_capability("session.observe")],
    )


    # --- XIORUN: browser runtime management + internal Colab callbacks
    from xiosync.subsystems.xiorun.api import router as xiorun_router
    # Internal /internal/xiorun/* callbacks have no RBAC — they validate by
    # XIOSYNC_INTERNAL_SECRET Bearer token. Session management routes
    # (/xiorun/sessions) reuse session.observe capability.
    application.include_router(
        xiorun_router, prefix="/api/v1",
    )

    # --- VAULT: universal encrypted secret store (org-scoped + platform-global)
    from xiosync.subsystems.vault.api import router as vault_router
    application.include_router(
        vault_router, prefix="/api/v1",
        dependencies=[require_capability("vault.read")],
    )

    # --- STORAGE: universal blob storage provider registry + object index
    from xiosync.subsystems.storage.api import router as storage_router
    application.include_router(
        storage_router, prefix="/api/v1",
        dependencies=[require_capability("storage.read")],
    )

    # --- IDENTITIES: universal external-identity + credential registry
    from xiosync.subsystems.identities.api import router as identities_router
    application.include_router(
        identities_router, prefix="/api/v1",
        dependencies=[require_capability("identities.read")],
    )

    # --- INTEGRATIONS: universal external connector registry
    from xiosync.subsystems.integrations.api import integrations_router
    application.include_router(
        integrations_router, prefix="/api/v1",
        dependencies=[require_capability("integrations.manage")],
    )

    # --- WORKER CONFIG: server-side config delivery (GET/PUT /workers/{id}/config)
    from xiosync.subsystems.integrations.api import worker_config_router
    application.include_router(
        worker_config_router, prefix="/api/v1",
        dependencies=[require_capability("worker.manage")],
    )

    from xiosync.api.routers.workers_crud import router as workers_crud_router
    application.include_router(
        workers_crud_router, prefix="/api/v1",
        dependencies=[require_capability("worker.manage")],
    )

    # --- WORKER BOOTSTRAP: secure one-URL config delivery for Colab workers
    # Admin routes (POST/GET /workers/bootstrap-tokens) require worker.manage.
    # Public route (GET /workers/bootstrap/{token}) has no RBAC — token = credential.
    from xiosync.api.routers.worker_bootstrap import router as worker_bootstrap_router
    application.include_router(
        worker_bootstrap_router, prefix="/api/v1",
    )

    # --- WORKER LOCKS: Redis-backed distributed lock API for Drive FUSE coordination
    from xiosync.api.routers.worker_locks import router as worker_locks_router
    application.include_router(
        worker_locks_router, prefix="/api/v1",
    )

    from xiosync.api.routers.secrets_crud import router as secrets_crud_router
    application.include_router(
        secrets_crud_router, prefix="/api/v1",
        dependencies=[require_capability("secret.manage")],
    )

    from xiosync.api.routers.shares import router as shares_router
    application.include_router(
        shares_router, prefix="/api/v1",
        dependencies=[require_capability("share.manage")],
    )

    from xiosync.api.routers.webhooks import router as webhooks_router
    application.include_router(
        webhooks_router, prefix="/api/v1",
        dependencies=[require_capability("webhook.manage")],
    )

    from xiosync.api.routers.ontology import router as ontology_router
    application.include_router(
        ontology_router, prefix="/api/v1",
        dependencies=[require_capability("ontology.manage")],
    )

    from xiosync.api.routers.operations import router as operations_router
    application.include_router(
        operations_router, prefix="/api/v1",
        dependencies=[require_capability("event.manage")],
    )

    # --- XIOGRID Decoupled Services ----------
    from xiosync.api.routers.browser_pools import router as browser_pools_router
    application.include_router(
        browser_pools_router, prefix="/api/v1",
        dependencies=[require_capability("browser_pool.manage")],
    )

    from xiosync.api.routers.browser_sessions import router as browser_sessions_router
    application.include_router(
        browser_sessions_router, prefix="/api/v1",
        dependencies=[require_capability("browser_session.manage")],
    )

    from xiosync.api.routers.compute_runtimes import router as compute_runtimes_router
    application.include_router(
        compute_runtimes_router, prefix="/api/v1",
        dependencies=[require_capability("compute_runtime.manage")],
    )

    from xiosync.api.routers.mesh_networks import router as mesh_networks_router
    application.include_router(
        mesh_networks_router, prefix="/api/v1",
        dependencies=[require_capability("mesh_network.manage")],
    )

    # --- Genesis Phase 3: Self-referential protocol (Gaps G-5, G-7) ----------
    from xiosync.api.routers.protocol import router as protocol_router
    application.include_router(
        protocol_router, prefix="/api/v1",
        dependencies=[require_capability("capability.manage")],
    )

    from xiosync.api.routers.capability_groups import router as capability_groups_router
    application.include_router(
        capability_groups_router, prefix="/api/v1",
        dependencies=[require_capability("capability.manage")],
    )

    # --- XIOGRID PPPoE Exit Nodes (browser_pool.manage capability) -----------
    from xiosync.subsystems.xiogrid.api.pppoe_nodes import router as pppoe_nodes_router
    application.include_router(
        pppoe_nodes_router, prefix="/api/v1",
        dependencies=[require_capability("browser_pool.manage")],
    )

    # Gap P-4: API version governance middleware.
    application.add_middleware(VersionGovernanceMiddleware)


    @application.exception_handler(RequestValidationError)
    async def validation_problem(request: Request, exc: RequestValidationError) -> JSONResponse:
        del exc
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/invalid_request",
                "title": "Invalid request",
                "status": 422,
                "code": "invalid_request",
                "request_id": request.state.request_id,
            },
        )

    # P2: QuotaExceededError → 429 Too Many Requests (RFC 7807).
    from xiosync.services.quotas import QuotaExceededError

    @application.exception_handler(QuotaExceededError)
    async def quota_problem(request: Request, exc: QuotaExceededError) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/quota_exceeded",
                "title": "Resource quota exceeded",
                "status": 429,
                "code": "quota_exceeded",
                "detail": str(exc),
                "resource_type": exc.resource_type,
                "current": exc.current,
                "limit": exc.limit,
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    # P2: InvalidEventError → 400 Bad Request (RFC 7807).
    from xiosync.domain.events import InvalidEventError

    @application.exception_handler(InvalidEventError)
    async def event_problem(request: Request, exc: InvalidEventError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/invalid_event",
                "title": "Invalid event",
                "status": 400,
                "code": "invalid_event",
                "detail": str(exc),
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    # P2: Catch-all for unhandled exceptions → 500 (RFC 7807).
    @application.exception_handler(Exception)
    async def unhandled_problem(request: Request, exc: Exception) -> JSONResponse:
        import logging
        logging.getLogger("xiosync.api").exception("unhandled_error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/internal_error",
                "title": "Internal server error",
                "status": 500,
                "code": "internal_error",
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    # Starlette wraps each newly-added middleware around the previous stack.
    # Add in reverse so the effective order is CORS -> request-id -> security -> size -> auth.
    # StrictCORSMiddleware is the outermost layer so preflight OPTIONS never hits auth.
    if cors_origins:
        application.add_middleware(StrictCORSMiddleware, allowed_origins=cors_origins)
    if rate_limiter is not None and rate_limit_config is not None:
        application.add_middleware(
            RateLimitMiddleware,
            rate_limiter=rate_limiter,
            config_fn=rate_limit_config,
        )
    application.add_middleware(
        AuthenticationMiddleware,
        session_service=session_service,
        engine=engine,
        clock=clock,
    )
    application.add_middleware(BodySizeLimitMiddleware, max_body_bytes=max_body_bytes)
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(RequestIDMiddleware)
    return application


def create_production_app() -> FastAPI:
    """Load validated configuration and wire production dependencies fail-fast (M5).

    Startup enforces strict validation:
    1. All environment variables must be present and valid (INV-CFG-1/2/3)
    2. Database must be connectable
    3. Database migrations must be at head revision (C6)

    If any check fails, the process exits non-zero before opening ports (INV-STARTUP-1).
    """
    # Step 1: Load and validate configuration (INV-CFG-1/2/3, INV-STARTUP-1)
    try:
        config = load_config()
    except ConfigError as exc:
        logger.critical(f"Configuration validation failed: {exc}")
        raise

    # Step 2: Configure logging with validated level
    configure_logging(config.log_level)
    logger.info(f"Starting XIOSYNC in {config.environment} environment")

    # Step 3: Create database engine (will fail fast if URL is invalid)
    try:
        engine = create_database_engine(config.database_url)
    except Exception as exc:
        logger.critical(f"Failed to create database engine: {exc}")
        raise

    # Step 4: Verify migrations are at head (C6, INV-STARTUP-1)
    # This MUST succeed before the app opens ports. If migrations are not applied,
    # startup fails immediately rather than starting a degraded service.
    try:
        verify_migrations_at_head(engine)
        logger.info("Database migrations verified at head")
    except Exception as exc:
        logger.critical(f"Migration verification failed (C6): {exc}")
        raise

    # Step 5: Wire remaining services
    service = SessionService(IdentityRepository(engine), config.auth_secret)
    logger.info("All startup checks passed; application ready")
    limiter = create_rate_limiter(config.redis_url) if config.redis_url else None
    config_fn = (
        (lambda route_class: get_rate_limit_config(route_class, config))
        if limiter
        else None
    )
    return create_app(
        session_service=service,
        engine=engine,
        clock=SystemClock(),
        rate_limiter=limiter,
        rate_limit_config=config_fn,
        cors_origins=config.cors_allowed_origins,
    )


class _LazyProductionApp:
    """Delay environment loading until ASGI startup while retaining fail-fast boot."""

    _application: FastAPI | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._application is None:
            self._application = create_production_app()
        await self._application(scope, receive, send)


app: Any = _LazyProductionApp()
