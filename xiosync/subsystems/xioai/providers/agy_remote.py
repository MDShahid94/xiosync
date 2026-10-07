from __future__ import annotations

import logging
import os

from xiosync.subsystems.xioai.providers.base import GenerationProvider, GenerationResult

logger = logging.getLogger(__name__)


class AGYRemoteProvider(GenerationProvider):
    @property
    def name(self) -> str:
        return "agy_remote"

    @classmethod
    def is_available(cls) -> bool:
        url = os.environ.get("XIOAI_REMOTE_URL")
        if not url:
            return False

        try:
            import httpx

            base = url.rstrip("/")
            # Try /ai/status first; fall back to /health (both exist on xiorun_agent)
            for path in ("/ai/status", "/health"):
                try:
                    resp = httpx.get(f"{base}{path}", timeout=5.0)
                    if resp.status_code == 200:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        output_format: str = "text",
        json_schema: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        timeout: int = 120,
    ) -> GenerationResult:
        url = os.environ.get("XIOAI_REMOTE_URL")
        if not url:
            return GenerationResult(
                success=False, error="XIOAI_REMOTE_URL not set", provider=self.name
            )

        try:
            import httpx

            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{url.rstrip('/')}/ai/generate",
                    json={
                        "prompt": prompt,
                        "system": system,
                        "output_format": output_format,
                        "json_schema": json_schema,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "timeout": timeout,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                return GenerationResult(
                    text=data.get("text", ""),
                    provider=data.get("provider", self.name),
                    model=data.get("model", ""),
                    success=data.get("success", True),
                    error=data.get("error"),
                    usage=data.get("usage", {}),
                )
        except Exception as exc:
            return GenerationResult(
                success=False,
                error=str(exc),
                provider=self.name,
            )
