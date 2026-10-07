"""ai_healer.py — Tier-10 LLM DOM exploration (AIHealer + provider implementations).

When all deterministic locator tiers fail, ``AIHealer.heal()`` asks an LLM to
analyse the page DOM and suggest a working CSS/XPath selector for the target
element.

The AIHealer internally uses xiosync.subsystems.xioai.gateway.AIGateway to
communicate with the optimal LLM provider.

Usage (in locator_cascade.py tier 10)
------
    from xiosync.subsystems.xioflow.engine.ai_healer import AIHealer
    healer = AIHealer()
    result = await healer.heal(page, intent="click login button", dom_inspector=get_dom)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from xiosync.subsystems.xioai.gateway import AIGateway

logger = logging.getLogger(__name__)

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

_DOM_TRUNCATE = 32_000  # chars — Context large but we clip to save tokens


class AIHealer:
    """Tier-10 LLM DOM exploration.

    Uses AIGateway to automatically select the best available LLM provider.
    """

    def __init__(self) -> None:
        self.gateway = AIGateway()

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
        try:
            # DOMInspector.get_interactive_elements() returns (dom_string, node_map)
            # We only need the dom_string for the LLM prompt
            if hasattr(dom_inspector, "get_interactive_elements"):
                dom_string, _node_map = await dom_inspector.get_interactive_elements()
            elif callable(dom_inspector):
                dom_string = await dom_inspector()
            else:
                logger.warning("ai_healer: dom_inspector is neither DOMInspector nor callable")
                return None
            truncated_dom = dom_string[:_DOM_TRUNCATE]
            prompt = f"Intent: {intent}\n\nDOM snapshot (may be truncated):\n{truncated_dom}"

            result = await self.gateway.generate(
                prompt, system=_SYSTEM_PROMPT, output_format="json"
            )

            if result.success:
                try:
                    parsed = json.loads(result.text)
                    if not isinstance(parsed, dict) or parsed.get("selector") is None:
                        return None

                    logger.info(
                        "ai_healer.success",
                        extra={
                            "intent": intent,
                            "selector": parsed.get("selector"),
                            "selector_type": parsed.get("selector_type"),
                            "confidence": parsed.get("confidence"),
                        },
                    )
                    return parsed
                except json.JSONDecodeError as exc:
                    logger.warning("ai_healer.json_parse_error", extra={"error": str(exc)})
            else:
                logger.warning("ai_healer.gateway_failed", extra={"intent": intent})
        except Exception as exc:
            logger.warning("ai_healer.heal_error", extra={"intent": intent, "error": str(exc)})

        return None
