"""ai_gateway.py — Universal pluggable AI provider gateway for XIOSYNC.

Provides a single ``AIGateway`` class that any XIOSYNC subsystem can use
for AI-powered generation tasks.  Organizations configure their preferred
provider via environment variables or DB settings.

Supported Provider Types
------------------------
**API-based (cloud-hosted):**
  - ``gemini``    — Google Gemini API (GEMINI_API_KEY)
  - ``openai``    — OpenAI API (OPENAI_API_KEY)
  - ``anthropic`` — Anthropic API (ANTHROPIC_API_KEY)

**Agent-based (CLI-invoked):**
  - ``agy``       — Antigravity CLI (local or remote)
  - ``codex``     — OpenAI Codex CLI

**Browser-based (XIOVIEW-routed):**
  - ``browser_ai`` — AI Studio / ChatGPT via XIOVIEW browser automation

**Self-hosted:**
  - ``ollama``    — Local Ollama models
  - ``custom``    — Any HTTP endpoint implementing the generation protocol

Selection Priority
------------------
1. Explicit ``provider`` argument
2. ``XIOSYNC_AI_PROVIDER`` env var (org-level)
3. Auto-detect: agy > gemini > openai > anthropic > ollama

Usage::

    from xiosync.subsystems.xioflow.services.ai_gateway import AIGateway

    gw = AIGateway()   # auto-detect best provider
    result = await gw.generate(
        prompt="Generate a workflow script for Google Sign-In",
        system="You are a XIOSYNC workflow engineer.",
        output_format="text",
    )
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import shutil
from typing import Any

logger = logging.getLogger(__name__)


# ── Abstract provider interface ───────────────────────────────────────────────


class GenerationProvider(abc.ABC):
    """Interface every AI generation provider must satisfy.

    This is a broader interface than ``LLMProvider`` in ai_healer.py:
    it supports arbitrary text generation, not just DOM exploration.
    """

    name: str = "base"

    @abc.abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        output_format: str = "text",  # "text" | "json"
        json_schema: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        timeout: int = 120,
    ) -> GenerationResult:
        """Generate text from the given prompt."""


class GenerationResult:
    """Standardized result from any provider."""

    __slots__ = ("text", "provider", "model", "success", "error", "usage")

    def __init__(
        self,
        text: str = "",
        provider: str = "",
        model: str = "",
        success: bool = True,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.text = text
        self.provider = provider
        self.model = model
        self.success = success
        self.error = error
        self.usage = usage or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "success": self.success,
            "error": self.error,
            "usage": self.usage,
        }


# ── AGY CLI Provider ─────────────────────────────────────────────────────────


class AGYProvider(GenerationProvider):
    """Antigravity CLI provider — invokes ``agy --print`` as a subprocess.

    This is the default provider for XIOSYNC when ``agy`` is installed.
    Supports structured JSON output via ``--json-schema`` and
    ``--output-format json``.

    Can be pointed to a remote agy instance via ``XIOSYNC_AGY_BIN``.
    """

    name = "agy"

    def __init__(
        self,
        agy_bin: str | None = None,
        model: str | None = None,
        effort: str = "high",
    ) -> None:
        self._bin = agy_bin or os.environ.get("XIOSYNC_AGY_BIN", "agy")
        self._model = model or os.environ.get("XIOSYNC_AGY_MODEL", "")
        self._effort = effort
        self._available = shutil.which(self._bin) is not None
        if not self._available:
            logger.warning("ai_gateway.agy_not_found", extra={"bin": self._bin})

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
        if not self._available:
            return GenerationResult(
                success=False,
                error="agy binary not found",
                provider=self.name,
            )

        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        cmd = [self._bin, f"--print={full_prompt}"]
        if output_format == "json":
            cmd.append("--output-format=json")
        if json_schema:
            cmd.append(f"--json-schema={json.dumps(json_schema)}")
        if self._model:
            cmd.append(f"--model={self._model}")
        cmd.append(f"--effort={self._effort}")
        cmd.append(f"--print-timeout={timeout}s")
        cmd.append("--dangerously-skip-permissions")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout + 10,
            )
            text = stdout.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[-500:]
                logger.warning(
                    "ai_gateway.agy_error", extra={"returncode": proc.returncode, "stderr": err}
                )
                return GenerationResult(
                    success=False,
                    error=f"agy exit {proc.returncode}: {err}",
                    provider=self.name,
                )

            return GenerationResult(
                text=text,
                provider=self.name,
                model=self._model or "agy-default",
                success=True,
            )

        except TimeoutError:
            return GenerationResult(
                success=False,
                error=f"agy timeout ({timeout}s)",
                provider=self.name,
            )
        except Exception as exc:
            return GenerationResult(
                success=False,
                error=str(exc),
                provider=self.name,
            )


# ── Gemini API Provider ──────────────────────────────────────────────────────


class GeminiAPIProvider(GenerationProvider):
    """Google Gemini API provider (google-generativeai SDK)."""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.0-flash",
    ) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._model_name = model
        self._client = None

        if not self._api_key:
            return

        try:
            import google.generativeai as genai

            genai.configure(api_key=self._api_key)
            self._client = genai
            logger.info("ai_gateway.gemini_ready", extra={"model": model})
        except ImportError:
            logger.warning("ai_gateway.gemini_not_installed")
        except Exception as exc:
            logger.warning("ai_gateway.gemini_init_error", extra={"error": str(exc)})

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
        if not self._client:
            return GenerationResult(
                success=False,
                error="Gemini not configured (no API key)",
                provider=self.name,
            )

        try:
            config = self._client.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                response_mime_type="application/json" if output_format == "json" else "text/plain",
            )
            model = self._client.GenerativeModel(
                model_name=self._model_name,
                system_instruction=system or None,
                generation_config=config,
            )
            response = await asyncio.to_thread(
                model.generate_content,
                prompt,
            )
            return GenerationResult(
                text=response.text.strip(),
                provider=self.name,
                model=self._model_name,
                success=True,
            )
        except Exception as exc:
            return GenerationResult(
                success=False,
                error=str(exc),
                provider=self.name,
                model=self._model_name,
            )


# ── OpenAI API Provider ──────────────────────────────────────────────────────


class OpenAIAPIProvider(GenerationProvider):
    """OpenAI Chat Completions provider."""

    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model
        self._client = None

        if not self._api_key:
            return
        try:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key)
            logger.info("ai_gateway.openai_ready", extra={"model": model})
        except ImportError:
            logger.warning("ai_gateway.openai_not_installed")

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
        if not self._client:
            return GenerationResult(
                success=False,
                error="OpenAI not configured",
                provider=self.name,
            )

        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if output_format == "json":
                kwargs["response_format"] = {"type": "json_object"}

            response = await self._client.chat.completions.create(**kwargs)
            text = response.choices[0].message.content or ""
            return GenerationResult(
                text=text.strip(),
                provider=self.name,
                model=self._model,
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
                model=self._model,
            )


# ── Custom HTTP Provider ─────────────────────────────────────────────────────


class CustomHTTPProvider(GenerationProvider):
    """Generic HTTP endpoint provider — any service implementing the protocol.

    Expects the endpoint to accept POST with:
        {"prompt": "...", "system": "...", "output_format": "text|json"}
    And return:
        {"text": "...", "model": "..."}

    Configure via XIOSYNC_AI_CUSTOM_URL env var.
    """

    name = "custom"

    def __init__(self, url: str | None = None) -> None:
        self._url = url or os.environ.get("XIOSYNC_AI_CUSTOM_URL", "")

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
        if not self._url:
            return GenerationResult(
                success=False,
                error="No custom AI URL configured",
                provider=self.name,
            )

        try:
            import httpx

            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    self._url,
                    json={
                        "prompt": prompt,
                        "system": system,
                        "output_format": output_format,
                        "json_schema": json_schema,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return GenerationResult(
                    text=data.get("text", ""),
                    provider=self.name,
                    model=data.get("model", "custom"),
                    success=True,
                )
        except Exception as exc:
            return GenerationResult(
                success=False,
                error=str(exc),
                provider=self.name,
            )


# ── Provider Registry ────────────────────────────────────────────────────────

_PROVIDER_REGISTRY: dict[str, type[GenerationProvider]] = {
    "agy": AGYProvider,
    "gemini": GeminiAPIProvider,
    "openai": OpenAIAPIProvider,
    "custom": CustomHTTPProvider,
}


def register_provider(name: str, cls: type[GenerationProvider]) -> None:
    """Register a custom AI provider for organizational use."""
    _PROVIDER_REGISTRY[name] = cls
    logger.info("ai_gateway.provider_registered", extra={"name": name})


# ── AIGateway — the main entry point ─────────────────────────────────────────


class AIGateway:
    """Universal AI generation gateway for XIOSYNC.

    Auto-detects the best available provider or uses the one specified
    by the organization via ``XIOSYNC_AI_PROVIDER`` env var.

    Usage::

        gw = AIGateway()                           # auto-detect
        gw = AIGateway(provider="gemini")           # explicit
        gw = AIGateway(provider=MyCustomProvider()) # instance

        result = await gw.generate("Write a workflow for ...", system="...")
        if result.success:
            print(result.text)
    """

    def __init__(
        self,
        provider: str | GenerationProvider | None = None,
    ) -> None:
        if isinstance(provider, GenerationProvider):
            self._provider = provider
        elif isinstance(provider, str):
            cls = _PROVIDER_REGISTRY.get(provider)
            if not cls:
                raise ValueError(
                    f"Unknown AI provider: {provider!r}. "
                    f"Available: {list(_PROVIDER_REGISTRY.keys())}"
                )
            self._provider = cls()
        else:
            self._provider = self._auto_detect()

    def _auto_detect(self) -> GenerationProvider:
        """Select the best available provider.

        Priority: env var > agy > gemini > openai > custom.
        """
        explicit = os.environ.get("XIOSYNC_AI_PROVIDER", "")
        if explicit and explicit in _PROVIDER_REGISTRY:
            logger.info("ai_gateway.provider_from_env", extra={"provider": explicit})
            return _PROVIDER_REGISTRY[explicit]()

        # Auto-detect in priority order
        if shutil.which("agy"):
            logger.info("ai_gateway.auto_detect", extra={"provider": "agy"})
            return AGYProvider()
        if os.environ.get("GEMINI_API_KEY"):
            logger.info("ai_gateway.auto_detect", extra={"provider": "gemini"})
            return GeminiAPIProvider()
        if os.environ.get("OPENAI_API_KEY"):
            logger.info("ai_gateway.auto_detect", extra={"provider": "openai"})
            return OpenAIAPIProvider()
        if os.environ.get("XIOSYNC_AI_CUSTOM_URL"):
            logger.info("ai_gateway.auto_detect", extra={"provider": "custom"})
            return CustomHTTPProvider()

        logger.warning(
            "ai_gateway.no_provider_available",
            extra={
                "advice": (
                    "Install agy CLI, set GEMINI_API_KEY, OPENAI_API_KEY, "
                    "or XIOSYNC_AI_CUSTOM_URL to enable AI generation."
                )
            },
        )
        # Return AGY as default — it will return a clean error if not installed
        return AGYProvider()

    @property
    def provider_name(self) -> str:
        return self._provider.name

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
        """Generate text using the configured AI provider."""
        return await self._provider.generate(
            prompt,
            system=system,
            output_format=output_format,
            json_schema=json_schema,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
