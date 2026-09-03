"""Bootstrap and organization API endpoints (Genesis Phase 0c — Gap G-1).

The bootstrap endpoint allows creating the genesis org via the API (with a
bootstrap token) for environments where CLI access is not possible. The
organization info endpoint exposes the current org's details.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["organizations"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapRequest(_StrictModel):
    bootstrap_token: str = Field(description="One-time bootstrap token from XIOSYNC_BOOTSTRAP_TOKEN env")
    admin_email: str = Field(default="admin@xiosync.dev", description="Admin email")
    admin_password: str | None = Field(default=None, description="Admin password (omit to skip)")


class BootstrapResponse(_StrictModel):
    organization_id: uuid.UUID
    system_actor_id: uuid.UUID
    human_actor_id: uuid.UUID
    ai_agent_actor_id: uuid.UUID
    auth_identity_id: uuid.UUID | None = None
    already_existed: bool


class OrganizationResponse(_StrictModel):
    id: uuid.UUID
    slug: str
    name: str
    state: str
    resource_quotas: dict[str, Any]
    external_providers: dict[str, Any] | None = None
    created_at: str


@router.post(
    "/bootstrap",
    response_model=BootstrapResponse,
    status_code=201,
    summary="Bootstrap XIOSYNC genesis (Gap G-1)",
)
def bootstrap_genesis(
    payload: BootstrapRequest,
    request: Request,
) -> BootstrapResponse | JSONResponse:
    """Bootstrap XIOSYNC as its own first organization via the API.

    Requires ``XIOSYNC_BOOTSTRAP_TOKEN`` to be set. This is a one-shot
    operation — subsequent calls with a valid token return the existing state.
    """
    expected_token = os.environ.get("XIOSYNC_BOOTSTRAP_TOKEN")
    if not expected_token:
        return JSONResponse(
            status_code=403,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/bootstrap_disabled",
                "title": "Bootstrap disabled",
                "status": 403,
                "code": "bootstrap_disabled",
                "detail": "XIOSYNC_BOOTSTRAP_TOKEN is not configured",
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    # Constant-time comparison to prevent timing attacks
    import hmac

    if not hmac.compare_digest(payload.bootstrap_token.encode(), expected_token.encode()):
        return JSONResponse(
            status_code=403,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/invalid_bootstrap_token",
                "title": "Invalid bootstrap token",
                "status": 403,
                "code": "invalid_bootstrap_token",
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SASession

    from xiosync.services.bootstrap import BootstrapService

    engine = request.app.state.engine
    with SASession(engine) as session:
        with session.begin():
            svc = BootstrapService(session)
            result = svc.genesis(
                admin_email=payload.admin_email,
                admin_password=payload.admin_password,
            )

    return BootstrapResponse(
        organization_id=result.organization_id,
        system_actor_id=result.system_actor_id,
        human_actor_id=result.human_actor_id,
        ai_agent_actor_id=result.ai_agent_actor_id,
        auth_identity_id=result.auth_identity_id,
        already_existed=result.already_existed,
    )


@router.get(
    "/organizations/current",
    response_model=OrganizationResponse,
    summary="Get current organization info",
)
def get_current_organization(
    request: Request,
) -> OrganizationResponse | JSONResponse:
    """Return details of the organization bound to the current auth context."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.persistence.models.identity import Organization

    context = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)

    org = session.execute(
        select(Organization).where(Organization.id == context.organization_id)
    ).scalar_one_or_none()

    if org is None:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "https://xiosync.dev/problems/organization_not_found",
                "title": "Organization not found",
                "status": 404,
                "code": "organization_not_found",
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    return OrganizationResponse(
        id=org.id,
        slug=org.slug,
        name=org.name,
        state=org.state,
        resource_quotas=org.resource_quotas or {},
        external_providers=org.external_providers,
        created_at=org.created_at.isoformat(),
    )

from xiosync.api.router_registry import register_router
register_router(
    router,
    prefix='/api/v1',
    tags=["organizations"],
)
