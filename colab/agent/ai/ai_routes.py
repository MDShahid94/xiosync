"""AI routes for xiorun-agent (/ai/status, /ai/generate, /ai/install-agy, /ai/restore-agy-creds)."""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

try:
    from agent.ai.agy_cli import (
        _AGY_BIN,
        _is_agy_authenticated,
        _is_agy_installed,
        _run_agy_local,
    )
except ImportError:
    try:
        from agent.ai.agy_cli import (
            _AGY_BIN,
            _is_agy_authenticated,
            _is_agy_installed,
            _run_agy_local,
        )
    except ImportError:
        pass

try:
    from agent.ai.agy_install import ai_install_agy, ai_restore_agy_creds
except ImportError:
    try:
        from agent.ai.agy_install import ai_install_agy, ai_restore_agy_creds
    except ImportError:
        pass

logger = logging.getLogger("xiorun_agent")

router = APIRouter()


class AIGenerateRequest(BaseModel):
    prompt: str
    system: str = ""
    output_format: str = "text"
    json_schema: str | None = None
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout: int = 120
    model: str | None = None
    provider: str | None = None  # "local", "remote", or None (auto)


@router.get("/ai/status")
async def ai_status() -> dict:
    """Check AI capabilities on this worker."""
    installed = _is_agy_installed()
    authenticated = _is_agy_authenticated() if installed else False
    version = None
    if installed:
        try:
            import subprocess as _sp

            version = _sp.check_output(
                [_AGY_BIN, "--version"],
                text=True,
                timeout=5,
                env={**os.environ, "DISPLAY": ""},
            ).strip()
        except Exception:
            pass

    # Check XIOSYNC server reachability for remote path
    xiosync_url = (
        os.environ.get("XIOSYNC_URL")
        or os.environ.get("XIORUN_XIOSYNC_BASE")
        or os.environ.get("XIOSYNC_BASE")
        or ""
    )
    remote_available = bool(xiosync_url)

    return {
        "agy_installed": installed,
        "agy_authenticated": authenticated,
        "agy_version": version,
        "agy_binary": _AGY_BIN if installed else None,
        "remote_available": remote_available,
        "xiosync_url": xiosync_url or None,
        "preferred_provider": (
            "agy_local" if authenticated else "agy_remote" if remote_available else "none"
        ),
    }


@router.post("/ai/generate")
async def ai_generate(req: AIGenerateRequest) -> dict:
    """Generate AI content — runs local agy or proxies to XIOSYNC server.

    Provider selection:
      - "local": Force local agy (must be installed + authenticated)
      - "remote": Force proxy to XIOSYNC server
      - None/auto: local if authenticated, otherwise remote
    """
    provider = req.provider

    # Auto-detect provider
    if not provider:
        if _is_agy_authenticated():
            provider = "local"
        else:
            provider = "remote"

    if provider == "local":
        if not _is_agy_installed():
            raise HTTPException(
                status_code=503,
                detail="agy not installed on this worker. Call POST /ai/install-agy first.",
            )
        if not _is_agy_authenticated():
            raise HTTPException(
                status_code=503, detail="agy not authenticated. Run agy auth flow first."
            )
        result = await _run_agy_local(
            req.prompt,
            system=req.system,
            output_format=req.output_format,
            json_schema=req.json_schema,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            timeout=req.timeout,
            model=req.model,
        )
        return result

    elif provider == "remote":
        # Proxy to XIOSYNC server's /api/v1/xioai/generate endpoint
        xiosync_url = (
            os.environ.get("XIOSYNC_URL")
            or os.environ.get("XIORUN_XIOSYNC_BASE")
            or os.environ.get("XIOSYNC_BASE")
            or ""
        )
        if not xiosync_url:
            raise HTTPException(
                status_code=503, detail="XIOSYNC_URL not configured — cannot proxy to remote AI."
            )

        import json as _json
        import urllib.request

        url = f"{xiosync_url}/api/v1/xioai/generate"
        body = _json.dumps(
            {
                "description": req.prompt,
                "system": req.system,
                "output_format": req.output_format,
                "json_schema": req.json_schema,
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
                "timeout": req.timeout,
                "model": req.model,
            }
        ).encode()

        # Use internal secret for auth if available
        headers = {"Content-Type": "application/json"}
        internal_secret = os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
        if internal_secret:
            headers["X-Internal-Secret"] = internal_secret

        try:
            http_req = urllib.request.Request(url, body, headers)
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: urllib.request.urlopen(http_req, timeout=req.timeout + 10).read(),
            )
            data = _json.loads(resp)
            # Map XIOSYNC response to our format
            if "text" in data:
                return {
                    "ok": True,
                    "text": data["text"],
                    "provider": f"remote:{data.get('provider', '?')}",
                    "model": data.get("model"),
                }
            elif "script" in data:
                return {
                    "ok": True,
                    "text": data["script"],
                    "provider": f"remote:{data.get('provider', '?')}",
                    "model": data.get("model"),
                }
            elif "error" in data or "detail" in data:
                return {
                    "ok": False,
                    "error": data.get("error") or data.get("detail"),
                    "provider": "remote",
                }
            else:
                return {"ok": True, "text": _json.dumps(data), "provider": "remote"}
        except Exception as e:
            return {"ok": False, "error": f"Remote AI call failed: {e}", "provider": "remote"}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {provider}. Use 'local', 'remote', or omit for auto.",
        )


@router.post("/ai/install-agy")
async def ai_install_agy_route() -> dict:
    """Install agy binary and authenticate using a persisted Google Chrome profile."""
    return await ai_install_agy()


@router.post("/ai/restore-agy-creds")
async def ai_restore_agy_creds_route() -> dict:
    """Restore persisted agy credentials from Drive (for runtime reboot recovery)."""
    return await ai_restore_agy_creds()
