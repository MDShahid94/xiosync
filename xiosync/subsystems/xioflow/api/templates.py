"""XIOFLOW workflow template API."""
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext
from xiosync.subsystems.xioflow.services.templates import (
    WorkflowTemplateNotFoundError,
    WorkflowTemplateService,
)

router = APIRouter(prefix="/xioflow/templates", tags=["xioflow-templates"])


# ── helpers ─────────────────────────────────────────────────────────────────

def _ctx(request: Request) -> OrgContext:
    return cast(OrgContext, request.state.org_context)


def _svc(request: Request) -> WorkflowTemplateService:
    session = cast(OrmSession, request.state.org_session)
    return WorkflowTemplateService(session)


# ── Request models ───────────────────────────────────────────────────────────

class TemplateCreate(BaseModel):
    name: str
    category: str | None = None
    description: str | None = None
    template_type: str = "script"
    script_ref: str = ""
    dag_domain: str | None = None
    dag_root_intent: str | None = None
    config: dict[str, Any] = {}
    project_id: uuid.UUID | None = None
    is_platform_global: bool = False


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("", summary="List workflow templates")
def list_templates(
    request: Request,
    category: str | None = None,
) -> dict[str, Any]:
    ctx = _ctx(request)
    svc = _svc(request)
    templates = svc.list_templates(ctx, category=category)
    return {
        "templates": [
            {
                "id": str(t.id),
                "name": t.name,
                "slug": t.slug,
                "category": t.category,
                "template_type": t.template_type,
                "script_ref": t.script_ref,
                "dag_domain": t.dag_domain,
                "dag_root_intent": t.dag_root_intent,
                "config": t.config,
                "is_platform_global": t.is_platform_global,
            }
            for t in templates
        ]
    }


@router.post("", summary="Register a workflow template", status_code=201)
def create_template(request: Request, body: TemplateCreate) -> dict[str, Any]:
    ctx = _ctx(request)
    svc = _svc(request)
    record = svc.register_template(
        ctx,
        name=body.name,
        category=body.category,
        description=body.description,
        script_ref=body.script_ref,
        template_type=body.template_type,
        dag_domain=body.dag_domain,
        dag_root_intent=body.dag_root_intent,
        config=body.config,
        project_id=body.project_id,
        is_platform_global=body.is_platform_global,
    )
    return {"id": str(record.id), "name": record.name, "slug": record.slug,
            "template_type": record.template_type}


@router.get("/{template_id}", summary="Get a workflow template")
def get_template(request: Request, template_id: uuid.UUID) -> dict[str, Any]:
    ctx = _ctx(request)
    svc = _svc(request)
    try:
        record = svc.get_template(ctx, template_id)
    except WorkflowTemplateNotFoundError:
        raise HTTPException(status_code=404, detail="template_not_found")
    return {"id": str(record.id), "name": record.name, "slug": record.slug,
            "template_type": record.template_type, "script_ref": record.script_ref,
            "config": record.config}


@router.delete("/{template_id}", summary="Delete a workflow template", status_code=204)
def delete_template(request: Request, template_id: uuid.UUID) -> None:
    ctx = _ctx(request)
    svc = _svc(request)
    try:
        svc.delete_template(ctx, template_id)
    except WorkflowTemplateNotFoundError:
        raise HTTPException(status_code=404, detail="template_not_found")
