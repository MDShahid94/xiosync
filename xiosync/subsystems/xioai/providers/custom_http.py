from __future__ import annotations

import logging
import os
from typing import Any

from xiosync.subsystems.xioai.providers.base import GenerationProvider, GenerationResult

logger = logging.getLogger(__name__)

class CustomHTTPProvider(GenerationProvider):
    @property
    def name(self) -> str:
        return "custom"

    @classmethod
    def is_available(cls) -> bool:
        return bool(os.environ.get("XIOAI_CUSTOM_URL"))

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
        url = os.environ.get("XIOAI_CUSTOM_URL")
        if not url:
            return GenerationResult(success=False, error="XIOAI_CUSTOM_URL not set", provider=self.name)
            
        try:
            import httpx
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    url,
                    json={
                        "prompt": prompt,
                        "system": system,
                        "output_format": output_format,
                        "json_schema": json_schema,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "timeout": timeout,
                    }
                )
                resp.raise_for_status()
                data = resp.json()
                
                return GenerationResult(
                    text=data.get("text", ""),
                    provider=data.get("provider", self.name),
                    model=data.get("model", "custom"),
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
