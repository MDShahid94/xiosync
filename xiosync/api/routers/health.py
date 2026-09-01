"""Health check endpoints for application lifecycle (M7).

Provides two distinct endpoints:
- /live: Returns 200 if the process is running (immediate response)
- /ready: Returns 200 only if the database is connected and migrations are at head

These endpoints are used by container orchestrators (Kubernetes, Docker Compose, etc.)
to determine if the service is healthy and ready to receive traffic.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from xiosync.core.health import check_readiness

router = APIRouter(tags=["health"])


@router.get(
    "/live",
    summary="Liveness probe",
    description="Returns 200 if the process is running. Used for basic health checks.",
    responses={
        200: {
            "description": "Process is alive",
            "content": {
                "application/json": {
                    "example": {"status": "alive", "message": "Process is running"}
                }
            },
        }
    },
)
async def liveness_probe() -> dict[str, str]:
    """Return 200 immediately to indicate the process is running (M7).

    This endpoint never fails and is used by orchestrators to detect process crashes.
    """
    return {"status": "alive", "message": "Process is running"}


@router.get(
    "/ready",
    summary="Readiness probe",
    description="Returns 200 only if the database is connected and migrations are at head.",
    responses={
        200: {
            "description": "Service is ready to receive traffic",
            "content": {
                "application/json": {"example": {"status": "ready", "message": "Fully operational"}}
            },
        },
        503: {
            "description": "Service is not ready (database issue or migrations not at head)",
            "content": {
                "application/json": {
                    "example": {
                        "status": "not_ready",
                        "message": "Database migrations not at head revision",
                    }
                }
            },
        },
    },
)
async def readiness_probe(request: Request) -> dict[str, str]:
    """Return 200 only if the database is connected and migrations are at head (M7, C6).

    This endpoint:
    1. Checks database connectivity
    2. Verifies migrations are at head revision
    3. Returns 503 if either check fails

    Orchestrators use this to route traffic only to ready instances.
    """
    engine = request.app.state.engine
    state = check_readiness(engine)

    if not state.is_ready:
        raise HTTPException(status_code=503, detail=state.ready_reason)

    return {"status": "ready", "message": "Fully operational"}
