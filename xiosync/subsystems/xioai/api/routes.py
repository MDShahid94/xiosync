from __future__ import annotations

from typing import Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from xiosync.subsystems.xioai.gateway import AIGateway

router = APIRouter(tags=["xioai"])

class GenerateRequest(BaseModel):
    description: str
    system: str = ""
    output_format: str = "text"
    json_schema: dict | None = None
    provider: str | None = None
    timeout: int = 120

@router.post("/generate")
async def generate(req: GenerateRequest) -> dict[str, Any]:
    gw = AIGateway(provider=req.provider)
    result = await gw.generate(
        prompt=req.description,
        system=req.system,
        output_format=req.output_format,
        json_schema=req.json_schema,
        timeout=req.timeout,
    )
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)
    return {
        "text": result.text,
        "provider": result.provider,
        "model": result.model,
        "usage": result.usage,
    }

@router.get("/providers")
async def get_providers() -> dict[str, Any]:
    gw = AIGateway()
    return {
        "available": AIGateway.available_providers(),
        "active": gw.provider_name,
    }

@router.get("/status")
async def get_status() -> dict[str, Any]:
    gw = AIGateway()
    return {
        "status": "ok",
        "active_provider": gw.provider_name,
    }
