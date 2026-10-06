from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from xiosync.subsystems.xioai.providers.base import GenerationProvider, GenerationResult

logger = logging.getLogger(__name__)

class GeminiProvider(GenerationProvider):
    @property
    def name(self) -> str:
        return "gemini"

    @classmethod
    def is_available(cls) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY"))

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
            return GenerationResult(success=False, error="GEMINI_API_KEY not set", provider=self.name)

        api_key = os.environ.get("GEMINI_API_KEY")
        model_name = os.environ.get("XIOAI_GEMINI_MODEL", "gemini-2.0-flash")

        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
        except ImportError:
            return GenerationResult(success=False, error="google.generativeai not installed", provider=self.name)
        
        full_text = system + prompt
        if len(full_text) > 100000:
            logger.warning("Prompt exceeds 100k characters, truncating...")
            # Truncating appropriately while keeping system if possible
            if len(system) > 50000:
                system = system[:50000]
            prompt = prompt[:100000 - len(system)]
            
        try:
            config = genai.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                response_mime_type="application/json" if output_format == "json" else "text/plain",
            )
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system or None,
                generation_config=config,
            )
            response = await asyncio.to_thread(
                model.generate_content, prompt,
            )
            return GenerationResult(
                text=response.text.strip(),
                provider=self.name,
                model=model_name,
                success=True,
            )
        except Exception as exc:
            return GenerationResult(
                success=False, error=str(exc), provider=self.name, model=model_name,
            )
