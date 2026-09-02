"""Capability group management API (improvement #7)."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["capability_groups"])


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/capability-groups", summary="List capability groups")
def list_groups(request: Request) -> list[dict[str, Any]]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.persistence.models.registry import CapabilityGroup

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    # Get global defaults (org_id IS NULL) + org-specific overrides
    stmt = (
        select(CapabilityGroup)
        .where(
            (CapabilityGroup.organization_id.is_(None))
            | (CapabilityGroup.organization_id == ctx.organization_id)
        )
        .order_by(CapabilityGroup.name)
    )
    rows = session.execute(stmt).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "description": r.description,
            "operations": r.operations,
            "state": r.state,
            "organization_id": str(r.organization_id) if r.organization_id else None,
        }
        for r in rows
    ]


@router.get("/capability-groups/{name}", summary="Get a capability group", response_model=None)
def get_group(name: str, request: Request) -> dict[str, Any] | JSONResponse:
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession

    from xiosync.domain.context import OrgContext
    from xiosync.persistence.models.registry import CapabilityGroup

    ctx = cast(OrgContext, request.state.org_context)
    session = cast(OrmSession, request.state.org_session)
    # Prefer org-specific override, fall back to global
    row = session.scalar(
        select(CapabilityGroup).where(
            CapabilityGroup.name == name,
            CapabilityGroup.organization_id == ctx.organization_id,
        )
    )
    if row is None:
        row = session.scalar(
            select(CapabilityGroup).where(
                CapabilityGroup.name == name,
                CapabilityGroup.organization_id.is_(None),
            )
        )
    if row is None:
        return JSONResponse(status_code=404, content={"detail": f"Group '{name}' not found"})
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "operations": row.operations,
        "state": row.state,
        "organization_id": str(row.organization_id) if row.organization_id else None,
    }
