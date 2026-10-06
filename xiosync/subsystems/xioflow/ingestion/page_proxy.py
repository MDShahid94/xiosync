"""PageProxy — transparent Patchright Page wrapper for auto-trace capture.

Intercepts every browser interaction method (click, fill, goto, etc.),
runs ``DOMInspector.inspect_target()`` to extract 9-tier locator data
*before* executing the action, then records the result into a
``TraceCollector``.

The script sees a standard Playwright/Patchright ``Page`` interface —
no code changes required.  All unrecognised attribute access is
delegated to the real page via ``__getattr__``.

Integration point:
    ``run_dispatcher.py`` wraps ``ctx.page`` with ``PageProxy`` when
    ``trace_mode=True``.  The proxy delegates every call to the real Page.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

from xiosync.subsystems.xioflow.ingestion.trace_collector import (
    TraceAction,
    TraceCollector,
)

logger = logging.getLogger(__name__)


class PageProxy:
    """Transparent proxy around a Patchright/Playwright Page object.

    Intercepts browser interaction methods to capture locator data
    via ``DOMInspector``, then delegates to the real page.

    Args:
        real_page: The actual Patchright ``Page`` object.
        trace_collector: ``TraceCollector`` instance to record actions into.
        dom_inspector: ``DOMInspector`` instance (optional; if None,
            locator capture is skipped but actions are still traced).
    """

    def __init__(
        self,
        real_page: Any,
        trace_collector: TraceCollector,
        dom_inspector: Any | None = None,
    ) -> None:
        self._page = real_page
        self._trace = trace_collector
        self._dom = dom_inspector

    # ── Intercepted methods ──────────────────────────────────────────────

    async def click(self, selector: str, **kwargs: Any) -> Any:
        """Intercept click — capture locators, execute, record."""
        locators, face = await self._capture(selector)
        t0 = time.monotonic()
        result = await self._page.click(selector, **kwargs)
        self._record("click", selector, locators, face, t0)
        return result

    async def fill(self, selector: str, value: str, **kwargs: Any) -> Any:
        """Intercept fill — capture locators, execute, record."""
        locators, face = await self._capture(selector)
        t0 = time.monotonic()
        result = await self._page.fill(selector, value, **kwargs)
        self._record("fill", selector, locators, face, t0,
                      action_params={"text": value})
        return result

    async def type(self, selector: str, text: str, **kwargs: Any) -> Any:
        """Intercept type (alias for fill in trace)."""
        locators, face = await self._capture(selector)
        t0 = time.monotonic()
        result = await self._page.type(selector, text, **kwargs)
        self._record("fill", selector, locators, face, t0,
                      action_params={"text": text})
        return result

    async def goto(self, url: str, **kwargs: Any) -> Any:
        """Intercept navigation."""
        t0 = time.monotonic()
        result = await self._page.goto(url, **kwargs)
        self._record("navigate", None, None, None, t0,
                      action_params={"url": url})
        return result

    async def wait_for_selector(self, selector: str, **kwargs: Any) -> Any:
        """Intercept wait_for_selector — record as a control action."""
        t0 = time.monotonic()
        result = await self._page.wait_for_selector(selector, **kwargs)
        self._record("wait_for_selector", selector, None, None, t0,
                      action_params={"selector": selector},
                      category="control")
        return result

    async def wait_for_load_state(self, state: str = "load", **kwargs: Any) -> Any:
        """Passthrough but record as control action."""
        t0 = time.monotonic()
        result = await self._page.wait_for_load_state(state, **kwargs)
        self._record("wait_for_load_state", None, None, None, t0,
                      action_params={"state": state},
                      category="control")
        return result

    async def press(self, selector: str, key: str, **kwargs: Any) -> Any:
        """Intercept keyboard press."""
        locators, face = await self._capture(selector)
        t0 = time.monotonic()
        result = await self._page.press(selector, key, **kwargs)
        self._record("press", selector, locators, face, t0,
                      action_params={"key": key})
        return result

    async def select_option(self, selector: str, values: Any = None, **kwargs: Any) -> Any:
        """Intercept dropdown selection."""
        locators, face = await self._capture(selector)
        t0 = time.monotonic()
        result = await self._page.select_option(selector, values, **kwargs)
        self._record("select_option", selector, locators, face, t0,
                      action_params={"values": values})
        return result

    async def check(self, selector: str, **kwargs: Any) -> Any:
        """Intercept checkbox check."""
        locators, face = await self._capture(selector)
        t0 = time.monotonic()
        result = await self._page.check(selector, **kwargs)
        self._record("check", selector, locators, face, t0)
        return result

    async def uncheck(self, selector: str, **kwargs: Any) -> Any:
        """Intercept checkbox uncheck."""
        locators, face = await self._capture(selector)
        t0 = time.monotonic()
        result = await self._page.uncheck(selector, **kwargs)
        self._record("uncheck", selector, locators, face, t0)
        return result

    async def hover(self, selector: str, **kwargs: Any) -> Any:
        """Intercept hover."""
        locators, face = await self._capture(selector)
        t0 = time.monotonic()
        result = await self._page.hover(selector, **kwargs)
        self._record("hover", selector, locators, face, t0)
        return result

    async def text_content(self, selector: str, **kwargs: Any) -> Any:
        """Intercept text extraction."""
        locators, face = await self._capture(selector)
        t0 = time.monotonic()
        result = await self._page.text_content(selector, **kwargs)
        self._record("extract_data", selector, locators, face, t0,
                      action_params={"extracted": result})
        return result

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        """Passthrough — evaluate() runs arbitrary JS, not a locator action."""
        return await self._page.evaluate(expression, *args, **kwargs)

    # ── Locator() passthrough (returns a LocatorProxy) ────────────────

    def locator(self, selector: str, **kwargs: Any) -> Any:
        """Return the real page's locator — no interception at this level.

        Individual locator actions (click, fill) will still go through
        the Patchright page's own execution path.
        """
        return self._page.locator(selector, **kwargs)

    # ── Property passthroughs ─────────────────────────────────────────

    @property
    def url(self) -> str:
        """Current page URL."""
        return self._page.url

    # ── Internal: locator capture ─────────────────────────────────────

    async def _capture(self, selector: str | None) -> tuple[dict | None, dict | None]:
        """Extract 9-tier locators for the target element via DOMInspector."""
        if not self._dom or not selector:
            return None, None
        try:
            result = await self._dom.inspect_target(self._page, selector)
            return result.get("place_value"), result.get("face_value")
        except Exception as e:
            logger.debug("PageProxy._capture failed for %r: %s", selector, e)
            return None, None

    def _record(
        self,
        action_type: str,
        selector: str | None,
        locators: dict | None,
        face: dict | None,
        t0: float,
        action_params: dict[str, Any] | None = None,
        category: str = "browser",
    ) -> None:
        """Record the action into TraceCollector."""
        url = getattr(self._page, "url", "")
        domain = urlparse(url).netloc if url else self._trace.domain
        params = action_params or {}
        if selector:
            params["original_selector"] = selector
        self._trace.record(TraceAction(
            category=category,
            action_type=action_type,
            place_value=locators,
            face_value=face,
            action_params=params,
            url=url,
            domain=domain,
            duration_ms=round((time.monotonic() - t0) * 1000, 1),
        ))

    # ── Passthrough for all other attributes ──────────────────────────

    def __getattr__(self, name: str) -> Any:
        """Delegate everything not intercepted to the real page.

        This ensures scripts using uncommon Playwright methods
        (e.g. ``page.title()``, ``page.content()``, ``page.screenshot()``)
        continue to work without modification.
        """
        return getattr(self._page, name)
