"""DOM Overlay mode — hybrid screencast + semantic DOM element hitboxes.

Combines CDP Screencast (visual JPEG frames) with periodic DOM element
bounding box extraction via CDP DOM.getBoxModel. The client renders
invisible interactive div hitboxes over the screencast canvas, enabling
element-aware clicking (snap-to-element) and accessibility inspection.

Sends to client:
  {"type": "dom_overlay", "elements": [...], "timestamp": ...}
  (Screencast frames are sent via the normal cdp_screencast_loop)

Each element in the array:
  {"nodeId": 123, "tag": "button", "id": "submit-btn", "classes": ["primary"],
   "text": "Sign In", "rect": {"x": 100, "y": 200, "width": 120, "height": 40},
   "interactive": true}

Security: No foreign JavaScript is injected. Uses only CDP commands.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# HTML tags that are typically interactive (clickable/typeable)
_INTERACTIVE_TAGS = frozenset({
    "a", "button", "input", "select", "textarea", "label",
    "details", "summary", "option", "dialog",
})

# CSS selectors for interactive elements
_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "tab", "menuitem", "switch", "slider",
})


async def dom_overlay_loop(
    session_id: str,
    queue: asyncio.Queue,
    get_page,
    *,
    interval_sec: float = 1.0,
    max_elements: int = 200,
) -> None:
    """Stream DOM element bounding boxes alongside screencast frames.

    Queries the CDP DOM tree, finds interactive elements, computes their
    bounding boxes, and pushes them as JSON to the viewer overlay.

    Args:
        session_id: Browser session ID.
        queue: Asyncio queue for overlay data messages.
        get_page: Async callable returning the Playwright Page, or None.
        interval_sec: Seconds between DOM scans (default 1.0).
        max_elements: Maximum elements to include per scan.
    """
    page = await get_page()
    if page is None:
        logger.warning("xioview.dom_overlay.no_page", extra={"session_id": session_id})
        return

    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")
    logger.info("xioview.dom_overlay.started", extra={"session_id": session_id})

    try:
        while True:
            try:
                elements = await _extract_interactive_elements(cdp, max_elements)

                msg = json.dumps({
                    "type": "dom_overlay",
                    "elements": elements,
                    "count": len(elements),
                    "timestamp": time.time(),
                })

                try:
                    queue.put_nowait(msg)
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    queue.put_nowait(msg)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("xioview.dom_overlay.scan_error", extra={
                    "session_id": session_id, "error": str(exc),
                })

            await asyncio.sleep(interval_sec)

    except asyncio.CancelledError:
        logger.info("xioview.dom_overlay.stopped", extra={"session_id": session_id})
    finally:
        try:
            await cdp.send("DOM.disable")
        except Exception:
            pass
        try:
            await cdp.detach()
        except Exception:
            pass


async def _extract_interactive_elements(
    cdp: Any, max_elements: int = 200,
) -> list[dict[str, Any]]:
    """Extract interactive DOM elements with bounding boxes via CDP.

    Returns a flat list of element descriptors with screen-space rects.
    """
    elements: list[dict[str, Any]] = []

    try:
        # Get the full DOM tree
        doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        root = doc.get("root", {})

        # Flatten the tree and collect interactive nodes
        _collect_interactive(root, elements, max_elements)

        # Resolve bounding boxes for collected elements
        for elem in elements:
            node_id = elem.get("nodeId")
            if node_id:
                try:
                    box_result = await cdp.send("DOM.getBoxModel", {"nodeId": node_id})
                    content = box_result.get("model", {}).get("content", [])
                    if len(content) >= 8:
                        # content is [x1,y1, x2,y2, x3,y3, x4,y4] (quad)
                        x = min(content[0], content[6])
                        y = min(content[1], content[3])
                        w = max(content[2], content[4]) - x
                        h = max(content[5], content[7]) - y
                        elem["rect"] = {
                            "x": round(x), "y": round(y),
                            "width": round(w), "height": round(h),
                        }
                except Exception:
                    # Element may be hidden/detached — skip its rect
                    pass

        # Filter out elements without rects (invisible/off-screen)
        elements = [e for e in elements if "rect" in e]

    except Exception as exc:
        logger.debug("xioview.dom_overlay.extract_error", extra={"error": str(exc)})

    return elements


def _collect_interactive(
    node: dict[str, Any],
    result: list[dict[str, Any]],
    max_count: int,
) -> None:
    """Recursively walk the DOM tree and collect interactive elements."""
    if len(result) >= max_count:
        return

    node_name = node.get("nodeName", "").lower()
    node_type = node.get("nodeType", 0)

    # Only process element nodes (type 1)
    if node_type == 1:
        attrs = _parse_attributes(node.get("attributes", []))
        is_interactive = (
            node_name in _INTERACTIVE_TAGS
            or attrs.get("role", "").lower() in _INTERACTIVE_ROLES
            or attrs.get("onclick") is not None
            or attrs.get("tabindex") is not None
            or "cursor" in attrs.get("style", "")
        )

        if is_interactive:
            node_id = node.get("nodeId")
            if node_id:
                text = ""
                # Try to get text content from first text child
                for child in node.get("children", []):
                    if child.get("nodeType") == 3:  # text node
                        text = (child.get("nodeValue", "") or "").strip()[:80]
                        break

                result.append({
                    "nodeId": node_id,
                    "tag": node_name,
                    "id": attrs.get("id", ""),
                    "classes": attrs.get("class", "").split() if attrs.get("class") else [],
                    "text": text,
                    "type": attrs.get("type", ""),
                    "role": attrs.get("role", ""),
                    "interactive": True,
                })

    # Recurse into children
    for child in node.get("children", []):
        if len(result) >= max_count:
            break
        _collect_interactive(child, result, max_count)

    # Recurse into shadow DOM
    shadow = node.get("shadowRoots", [])
    for sr in shadow:
        if len(result) >= max_count:
            break
        _collect_interactive(sr, result, max_count)


def _parse_attributes(attrs: list) -> dict[str, str]:
    """Convert CDP's flat attribute list [name, value, name, value, ...] to a dict."""
    result: dict[str, str] = {}
    for i in range(0, len(attrs) - 1, 2):
        result[attrs[i]] = attrs[i + 1]
    return result
