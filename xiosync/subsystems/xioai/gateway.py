from __future__ import annotations

import logging
import os
import pathlib
from typing import Any

from xiosync.subsystems.xioai.providers.agy_local import AGYLocalProvider
from xiosync.subsystems.xioai.providers.agy_remote import AGYRemoteProvider
from xiosync.subsystems.xioai.providers.base import GenerationProvider, GenerationResult
from xiosync.subsystems.xioai.providers.custom_http import CustomHTTPProvider
from xiosync.subsystems.xioai.providers.gemini import GeminiProvider
from xiosync.subsystems.xioai.providers.openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)


def _load_dotenv_stdlib() -> None:
    """Load XIOAI_* vars from .env using stdlib only (no python-dotenv dependency).

    Walks up from this file's location to find the project root .env.
    Variables already set in os.environ are never overridden (shell wins over .env).
    """
    try:
        _start = pathlib.Path(__file__).resolve().parent
        for _parent in [_start, *_start.parents]:
            _dotenv = _parent / ".env"
            if _dotenv.exists():
                for _line in _dotenv.read_text(encoding="utf-8").splitlines():
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _, _v = _line.partition("=")
                    _k = _k.strip()
                    _v = _v.strip().strip('"').strip("'")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
                break
    except Exception:
        pass


# Auto-load at import time so providers see XIOAI_* vars even when the server
# process was launched without explicitly sourcing .env.
_load_dotenv_stdlib()


_PROVIDER_REGISTRY: dict[str, type[GenerationProvider]] = {
    "agy_local": AGYLocalProvider,
    "agy_remote": AGYRemoteProvider,
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "custom": CustomHTTPProvider,
}


def register_provider(name: str, cls: type[GenerationProvider]) -> None:
    _PROVIDER_REGISTRY[name] = cls
    logger.info("xioai.gateway: provider registered: %s", name)


class AIGateway:
    def __init__(self, provider: str | None = None) -> None:
        self._provider_name = provider or os.environ.get("XIOAI_PROVIDER")
        self._provider = self._init_provider()

    def _init_provider(self) -> GenerationProvider:
        if self._provider_name and self._provider_name in _PROVIDER_REGISTRY:
            return _PROVIDER_REGISTRY[self._provider_name]()

        # Probe in priority order
        if AGYLocalProvider.is_available():
            return AGYLocalProvider()
        if AGYRemoteProvider.is_available():
            return AGYRemoteProvider()
        if GeminiProvider.is_available():
            return GeminiProvider()
        if OpenAIProvider.is_available():
            return OpenAIProvider()
        if CustomHTTPProvider.is_available():
            return CustomHTTPProvider()

        logger.warning("xioai.gateway.no_provider_available")
        return AGYLocalProvider()

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @classmethod
    def available_providers(cls) -> dict[str, bool]:
        return {
            name: provider_cls.is_available() for name, provider_cls in _PROVIDER_REGISTRY.items()
        }

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
        **kwargs: Any,
    ) -> GenerationResult:
        try:
            return await self._provider.generate(
                prompt,
                system=system,
                output_format=output_format,
                json_schema=json_schema,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except Exception as exc:
            logger.error(
                "ai_gateway.generate_error",
                extra={
                    "provider": self.provider_name,
                    "error": str(exc),
                },
            )
            return GenerationResult(
                success=False,
                error=str(exc),
                provider=self.provider_name,
            )
