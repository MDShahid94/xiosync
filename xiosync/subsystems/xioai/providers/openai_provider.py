from __future__ import annotations

import logging
import os
from typing import Any

from xiosync.subsystems.xioai.providers.base import GenerationProvider, GenerationResult

logger = logging.getLogger(__name__)


class OpenAIProvider(GenerationProvider):
    @property
    def name(self) -> str:
        return "openai"

    @classmethod
    def is_available(cls) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

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
        if not self.is_available():
            return GenerationResult(
                success=False, error="OPENAI_API_KEY not set", provider=self.name
            )

        api_key = os.environ.get("OPENAI_API_KEY")
        model_name = os.environ.get("XIOAI_OPENAI_MODEL", "gpt-4o-mini")

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key, timeout=timeout)
        except ImportError:
            return GenerationResult(
                success=False, error="openai SDK not installed", provider=self.name
            )

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if output_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = await client.chat.completions.create(**kwargs)
            text = response.choices[0].message.content or ""
            return GenerationResult(
                text=text.strip(),
                provider=self.name,
                model=model_name,
                success=True,
                usage={
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                }
                if response.usage
                else {},
            )
        except Exception as exc:
            return GenerationResult(
                success=False,
                error=str(exc),
                provider=self.name,
                model=model_name,
            )
