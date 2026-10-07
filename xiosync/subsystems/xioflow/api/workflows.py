"""XIOFLOW Workflows API — AI-powered workflow generation and management.

Endpoints:
  POST /xioflow/workflows/generate     Generate a workflow script from description
  GET  /xioflow/workflows/providers    List available AI providers
"""

from __future__ import annotations

import logging
import os
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from xiosync.domain.context import OrgContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xioflow/workflows", tags=["XIOFLOW Workflows"])


# ── Request / Response models ─────────────────────────────────────────────────


class GenerateRequest(BaseModel):
    """Generate a workflow script from a human description."""

    description: str  # e.g. "Sign in to Slack with Google SSO"
    target_domain: str = ""  # e.g. "slack.com"
    params: dict[str, str] | None = None  # Expected input params
    reference_script: str | None = None  # Existing script to use as reference
    provider: str | None = None  # Override AI provider (agy, gemini, openai, custom)
    save: bool = False  # Save to tools/workflows/generated/
    timeout: int = 120  # Generation timeout in seconds
    model_config = ConfigDict(from_attributes=True)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ctx(request: Request) -> OrgContext:
    return cast(OrgContext, request.state.org_context)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/generate", summary="Generate a workflow script from description")
async def generate_workflow(request: Request, body: GenerateRequest) -> dict[str, Any]:
    """AI-powered workflow script generation.

    Uses the organization's configured AI provider (or auto-detects).
    Returns the generated .mjs source code and metadata.
    """
    from xiosync.subsystems.xioflow.services.workflow_generator import WorkflowGenerator

    ctx = _ctx(request)
    generator = WorkflowGenerator(provider=body.provider)

    if body.save:
        result = await generator.generate_and_save(
            description=body.description,
            target_domain=body.target_domain,
            params=body.params,
            reference_script=body.reference_script,
            timeout=body.timeout,
        )
    else:
        result = await generator.generate(
            description=body.description,
            target_domain=body.target_domain,
            params=body.params,
            reference_script=body.reference_script,
            timeout=body.timeout,
        )

    if not result.source_code:
        raise HTTPException(
            status_code=502,
            detail=f"AI generation failed: {result.meta.get('error', 'unknown error')}",
        )

    logger.info(
        "workflows_api.generated",
        extra={
            "org_id": str(ctx.organization_id),
            "script_ref": result.script_ref,
            "provider": result.provider,
            "saved": result.saved,
        },
    )

    return {
        "script_ref": result.script_ref,
        "source": result.source_code,
        "meta": result.meta,
        "provider": result.provider,
        "model": result.model,
        "saved": result.saved,
    }


@router.get("/providers", summary="List available AI providers")
async def list_providers() -> dict[str, Any]:
    """List all available AI generation providers and their status."""
    import shutil

    from xiosync.subsystems.xioai.gateway import _PROVIDER_REGISTRY

    providers = []
    for name, cls in _PROVIDER_REGISTRY.items():
        status = "unavailable"
        detail = ""

        if name == "agy":
            if shutil.which("agy"):
                status = "available"
                detail = shutil.which("agy") or ""
            else:
                detail = "agy CLI not found in PATH"
        elif name == "gemini":
            if os.environ.get("GEMINI_API_KEY"):
                status = "available"
                detail = "API key configured"
            else:
                detail = "Set GEMINI_API_KEY"
        elif name == "openai":
            if os.environ.get("OPENAI_API_KEY"):
                status = "available"
                detail = "API key configured"
            else:
                detail = "Set OPENAI_API_KEY"
        elif name == "custom":
            url = os.environ.get("XIOSYNC_AI_CUSTOM_URL", "")
            if url:
                status = "available"
                detail = url
            else:
                detail = "Set XIOSYNC_AI_CUSTOM_URL"

        providers.append(
            {
                "name": name,
                "status": status,
                "detail": detail,
            }
        )

    # Detect which would be auto-selected
    auto = os.environ.get("XIOSYNC_AI_PROVIDER", "")
    if not auto:
        for p in providers:
            if p["status"] == "available":
                auto = p["name"]
                break

    return {
        "providers": providers,
        "auto_selected": auto,
        "env_override": os.environ.get("XIOSYNC_AI_PROVIDER", ""),
    }
