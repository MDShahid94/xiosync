from __future__ import annotations

from typing import Any

import structlog

from xiosync.subsystems.xioflow.engine.ai_healer import AIHealer
from xiosync.subsystems.xioflow.engine.vision_matcher import VisionMatcher

logger = structlog.get_logger(__name__)

class LocatorCascade:
    """The 10-Tier Stability-Ordered Locator Cascade."""

    def __init__(self, page: Any):
        self.page = page

    async def resolve(
        self,
        place_value: dict,
        face_value: dict,
        action: str,
        params: dict,
        locator_priority: list[int]
    ) -> tuple[bool, int | None, str | None]:
        """Resolve an element through multiple tiers and act on it."""
        for tier in locator_priority:
            try:
                success, locator = await self._try_tier(tier, place_value, face_value, action, params)
                if success:
                    logger.info("locator_resolved", tier=tier, locator=locator)
                    return True, tier, locator
            except Exception as e:
                logger.debug("locator_tier_failed", tier=tier, error=str(e))

        return False, None, None

    async def _try_tier(self, tier: int, place_value: dict, face_value: dict, action: str, params: dict) -> tuple[bool, str | None]:
        locator_str = None
        timeout = 2000

        if tier == 1:
            # test_id
            test_ids = ["data-testid", "data-test-id", "data-test", "data-cy", "data-automation-id", "data-qa"]
            val = place_value.get("test_id")
            if val:
                for attr in test_ids:
                    locator_str = f"[{attr}='{val}']"
                    try:
                        await self._perform_action(locator_str, action, params, timeout)
                        return True, locator_str
                    except Exception:
                        pass
                return False, None

        elif tier == 2:
            # aria
            label = place_value.get("aria_label")
            if label:
                locator_str = f"[aria-label='{label}']"
                await self._perform_action(locator_str, action, params, timeout)
                return True, locator_str

        elif tier == 3:
            # axes_xpath
            axes_xpath = place_value.get("axes_xpath", [])
            for xp in axes_xpath:
                try:
                    await self._perform_action(xp, action, params, timeout)
                    return True, xp
                except Exception:
                    pass
            return False, None

        elif tier == 4:
            # anchor_text
            anchor = place_value.get("anchor_text")
            if anchor:
                locator_str = f":right-of(:text('{anchor}'))"
                await self._perform_action(locator_str, action, params, timeout)
                return True, locator_str

        elif tier == 5:
            # inner_text/text
            text = face_value.get("text")
            if text:
                locator_str = f"text='{text}'"
                await self._perform_action(locator_str, action, params, timeout)
                return True, locator_str

        elif tier == 6:
            # CSS selector
            css = place_value.get("css")
            if css:
                locator_str = css
                await self._perform_action(locator_str, action, params, timeout)
                return True, locator_str

        elif tier == 7:
            # standard xpath
            xpath = place_value.get("xpath")
            if xpath:
                locator_str = xpath
                await self._perform_action(locator_str, action, params, timeout)
                return True, locator_str

        elif tier == 8:
            # coordinate click
            box = face_value.get("bounding_box")
            if box and action == "click":
                x = box.get("nx", 0) + box.get("nw", 0)/2
                y = box.get("ny", 0) + box.get("nh", 0)/2
                await self.page.mouse.click(x, y)
                return True, f"coordinates: {x},{y}"

        elif tier == 9:
            # OpenCV visual match
            full_page = params.get("full_page_b64")
            template = params.get("template_b64")
            if full_page and template:
                match = VisionMatcher.match_template(full_page, template)
                if match.get("success"):
                    if action == "click":
                        await self.page.mouse.click(match["x"], match["y"])
                    return True, "vision_matcher"

        elif tier == 10:
            # AI healer
            healer = AIHealer()
            intent = params.get("intent")
            dom_inspector = params.get("dom_inspector")
            if intent and dom_inspector:
                result = await healer.heal(self.page, intent, dom_inspector)
                if result:
                    loc = result.get("locator")
                    if loc:
                        await self._perform_action(loc, action, params, timeout)
                        return True, loc

        return False, None

    async def _perform_action(self, locator: str, action: str, params: dict, timeout: int) -> None:
        loc = self.page.locator(locator).first
        if action == "click":
            await loc.click(timeout=timeout)
        elif action == "fill":
            await loc.fill(params.get("text", ""), timeout=timeout)
        elif action == "text_content":
            await loc.text_content(timeout=timeout)
