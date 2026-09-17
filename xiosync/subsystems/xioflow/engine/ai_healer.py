"""ai_healer.py — Tier-10 LLM DOM exploration (AIHealer + provider implementations).

When all deterministic locator tiers fail, ``AIHealer.heal()`` asks an LLM to
analyse the page DOM and suggest a working CSS/XPath selector for the target
element.

Providers
---------
GeminiLLMProvider   — Google Gemini API (``GEMINI_API_KEY`` env var)
                       Model: gemini-2.0-flash (fast + cheap for DOM tasks)
OpenAILLMProvider   — OpenAI Chat Completions (``OPENAI_API_KEY`` env var, optional)

The provider is selected at construction time.  ``AIHealer`` falls back
gracefully (returns ``None``) when no provider is configured.

Usage (in locator_cascade.py tier 10)
------
    from xiosync.subsystems.xioflow.engine.ai_healer import AIHealer, GeminiLLMProvider
    healer = AIHealer(provider=GeminiLLMProvider())
    result = await healer.heal(page, intent="click login button", dom_inspector=get_dom)
"""
from __future__ import annotations

import abc
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# ── Abstract provider ──────────────────────────────────────────────────────────

class LLMProvider(abc.ABC):
    """Interface every LLM provider must satisfy."""

    @abc.abstractmethod
    async def explore_dom(self, dom_string: str, intent: str) -> dict | None:
        """Return a dict with ``selector`` and ``selector_type`` keys, or None."""


# ── Gemini provider ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert web automation assistant.
You receive a compressed HTML DOM snapshot and a description of what the user
wants to interact with (the "intent").

Your task:
  1. Find the element that best matches the intent.
  2. Return a JSON object with exactly these fields:
       {
         "selector":      "<CSS or XPath selector string>",
         "selector_type": "css" | "xpath",
         "confidence":    <float 0.0-1.0>,
         "reasoning":     "<one sentence why this selector works>"
       }
  3. If you cannot find a suitable element, return: {"selector": null}

Rules:
- Prefer CSS selectors; use XPath only for complex ancestors.
- Prefer stable attributes (id, data-testid, aria-label, name) over positional
  selectors (nth-child) which break on layout changes.
- The DOM may be truncated; still do your best.
- Output ONLY the JSON object — no markdown, no prose.
"""

_GEMINI_MODEL = "gemini-2.0-flash"
_DOM_TRUNCATE  = 32_000   # chars — Gemini flash context is large but we clip to save tokens


class GeminiLLMProvider(LLMProvider):
    """Google Gemini LLM provider for DOM exploration.

    Requires the ``google-generativeai`` package and the ``GEMINI_API_KEY``
    environment variable (or pass ``api_key`` explicitly).
    """

    def __init__(self, api_key: str | None = None, model: str = _GEMINI_MODEL) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._model   = model
        self._client  = None

        if not self._api_key:
            logger.warning(
                "ai_healer.gemini_no_api_key",
                extra={"advice": "Set GEMINI_API_KEY to enable LLM-based DOM healing (Tier 10)."},
            )
            return

        try:
            import google.generativeai as genai  # noqa: PLC0415
            genai.configure(api_key=self._api_key)
            self._client = genai.GenerativeModel(
                model_name=self._model,
                system_instruction=_SYSTEM_PROMPT,
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.0,       # deterministic — we want reliable selectors
                    max_output_tokens=256,
                ),
            )
            logger.info("ai_healer.gemini_ready", extra={"model": self._model})
        except ImportError:
            logger.warning(
                "ai_healer.google_generativeai_not_installed",
                extra={"advice": "pip install google-generativeai to enable Gemini Tier-10 healing."},
            )
        except Exception as exc:
            logger.warning("ai_healer.gemini_init_failed", extra={"error": str(exc)})

    async def explore_dom(self, dom_string: str, intent: str) -> dict | None:
        if not self._client:
            return None

        truncated_dom = dom_string[:_DOM_TRUNCATE]
        prompt = (
            f"Intent: {intent}\n\n"
            f"DOM snapshot (may be truncated):\n{truncated_dom}"
        )

        try:
            import asyncio  # noqa: PLC0415
            loop = asyncio.get_event_loop()
            # Gemini Python SDK is sync — run in thread pool to avoid blocking event loop
            response = await loop.run_in_executor(
                None,
                lambda: self._client.generate_content(prompt),
            )
            raw = response.text.strip()
            result = json.loads(raw)

            if not isinstance(result, dict):
                return None
            if result.get("selector") is None:
                return None

            logger.info(
                "ai_healer.gemini_result",
                extra={
                    "intent":        intent,
                    "selector":      result.get("selector"),
                    "selector_type": result.get("selector_type"),
                    "confidence":    result.get("confidence"),
                },
            )
            return result

        except json.JSONDecodeError as exc:
            logger.warning("ai_healer.gemini_json_parse_error", extra={"error": str(exc)})
            return None
        except Exception as exc:
            logger.warning("ai_healer.gemini_api_error", extra={"error": str(exc)})
            return None


# ── OpenAI provider (optional) ─────────────────────────────────────────────────

class OpenAILLMProvider(LLMProvider):
    """OpenAI Chat Completions provider — optional fallback when Gemini unavailable.

    Requires ``openai`` package and ``OPENAI_API_KEY`` env var.
    """

    def __init__(self, api_key: str | None = None, model: str = "gpt-4o-mini") -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model   = model
        self._client  = None

        if not self._api_key:
            logger.warning("ai_healer.openai_no_api_key")
            return

        try:
            from openai import AsyncOpenAI  # noqa: PLC0415
            self._client = AsyncOpenAI(api_key=self._api_key)
            logger.info("ai_healer.openai_ready", extra={"model": self._model})
        except ImportError:
            logger.warning("ai_healer.openai_not_installed",
                           extra={"advice": "pip install openai to use OpenAI Tier-10 healing."})
        except Exception as exc:
            logger.warning("ai_healer.openai_init_failed", extra={"error": str(exc)})

    async def explore_dom(self, dom_string: str, intent: str) -> dict | None:
        if not self._client:
            return None

        truncated_dom = dom_string[:_DOM_TRUNCATE]
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": f"Intent: {intent}\n\nDOM:\n{truncated_dom}"},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=256,
            )
            raw = response.choices[0].message.content or ""
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("selector") is None:
                return None
            return result
        except Exception as exc:
            logger.warning("ai_healer.openai_api_error", extra={"error": str(exc)})
            return None


# ── AIHealer orchestrator ──────────────────────────────────────────────────────

def _make_default_provider() -> LLMProvider | None:
    """Build the best available provider from environment variables."""
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiLLMProvider()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAILLMProvider()
    logger.warning(
        "ai_healer.no_provider_configured",
        extra={
            "advice": (
                "Set GEMINI_API_KEY (preferred) or OPENAI_API_KEY to enable "
                "LLM-based DOM healing (Tier 10).  Without a provider, AIHealer "
                "always returns None and Tier 10 is skipped."
            )
        },
    )
    return None


class AIHealer:
    """Tier-10 LLM DOM exploration.

    Automatically selects the best available LLM provider unless one is
    supplied explicitly.  Pass ``provider=None`` explicitly to disable.
    """

    def __init__(self, provider: LLMProvider | None = "auto") -> None:  # type: ignore[assignment]
        if provider == "auto":
            self.provider = _make_default_provider()
        else:
            self.provider = provider

    async def heal(
        self,
        page: Any,
        intent: str,
        dom_inspector: Any,
    ) -> dict | None:
        """Use LLM to explore the page DOM and return a working selector.

        Parameters
        ----------
        page         : live Playwright Page object
        intent       : human-readable description of the target element
        dom_inspector: zero-arg async callable that returns the DOM as a string

        Returns a dict with keys ``selector`` and ``selector_type`` or None.
        """
        if not self.provider:
            logger.debug("ai_healer.skipped_no_provider", extra={"intent": intent})
            return None

        try:
            dom_string = await dom_inspector()
            result = await self.provider.explore_dom(dom_string, intent)
            if result:
                logger.info("ai_healer.success", extra={"intent": intent})
                return result
        except Exception as exc:
            logger.warning("ai_healer.heal_error", extra={"intent": intent, "error": str(exc)})

        return None
