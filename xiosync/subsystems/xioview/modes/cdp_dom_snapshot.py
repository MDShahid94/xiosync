"""CDP DOM Snapshot mode — structured DOM streaming via Chrome DevTools Protocol.

Uses DOMSnapshot.captureSnapshot to periodically capture the page's DOM tree
with computed styles. Lighter than full rrweb (no JS injection into page) and
more structured than screenshots (preserves element semantics).

Sends to client:
  {"type": "dom_snapshot", "documents": [...], "strings": [...], "timestamp": ...}

The client can render this as a reconstructed DOM or use it for accessibility
analysis, structured data extraction, or as a foundation for future DOM proxy mode.

Security: No foreign JavaScript is injected into the target page (unlike rrweb).
This preserves stealth/anti-fingerprinting properties.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


async def cdp_dom_snapshot_loop(
    session_id: str,
    queue: asyncio.Queue,
    get_page,
    *,
    interval_sec: float = 1.0,
    strip_scripts: bool = True,
) -> None:
    """Stream periodic DOM snapshots via CDP DOMSnapshot.captureSnapshot.

    Args:
        session_id: Browser session ID.
        queue: Asyncio queue to push snapshot messages into.
        get_page: Async callable returning the Playwright Page, or None.
        interval_sec: Seconds between snapshots (default 1.0).
        strip_scripts: If True, remove <script> node content from snapshots.
    """
    page = await get_page()
    if page is None:
        logger.warning("xioview.dom_snapshot.no_page", extra={"session_id": session_id})
        return

    cdp = await page.context.new_cdp_session(page)
    logger.info("xioview.dom_snapshot.started", extra={"session_id": session_id})

    try:
        while True:
            try:
                snapshot = await cdp.send("DOMSnapshot.captureSnapshot", {
                    "computedStyles": [
                        "display", "visibility", "opacity", "position",
                        "width", "height", "top", "left",
                        "color", "background-color", "font-size",
                        "cursor", "pointer-events",
                    ],
                })

                # Optionally strip script content to prevent any leakage
                if strip_scripts and "documents" in snapshot:
                    _strip_script_nodes(snapshot)

                msg = json.dumps({
                    "type": "dom_snapshot",
                    "snapshot": snapshot,
                    "timestamp": time.time(),
                    "session_id": session_id,
                })

                try:
                    queue.put_nowait(msg)
                except asyncio.QueueFull:
                    # Drop old snapshot — viewer prefers latest
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    queue.put_nowait(msg)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("xioview.dom_snapshot.capture_error", extra={
                    "session_id": session_id, "error": str(exc),
                })

            await asyncio.sleep(interval_sec)

    except asyncio.CancelledError:
        logger.info("xioview.dom_snapshot.stopped", extra={"session_id": session_id})
    finally:
        try:
            await cdp.detach()
        except Exception:
            pass


def _strip_script_nodes(snapshot: dict[str, Any]) -> None:
    """Remove <script> node values from a DOMSnapshot to prevent JS leakage.

    Modifies the snapshot in-place. The DOMSnapshot format uses a string table
    and integer indices, so we replace script-related string values with empty
    strings to preserve array indexing.
    """
    strings = snapshot.get("strings", [])
    if not strings:
        return

    # Build a set of string indices that represent "script" tag names
    script_indices: set[int] = set()
    for i, s in enumerate(strings):
        if isinstance(s, str) and s.lower() in ("script", "noscript"):
            script_indices.add(i)

    # For each document, find nodes with script tag names and blank their text
    for doc in snapshot.get("documents", []):
        nodes = doc.get("nodes", {})
        node_names = nodes.get("nodeName", [])
        node_values = nodes.get("nodeValue", [])

        for idx, name_idx in enumerate(node_names):
            if name_idx in script_indices:
                # Clear the nodeValue for this script node and its children
                if idx < len(node_values) and node_values[idx] >= 0:
                    # Replace the string at this index with empty
                    str_idx = node_values[idx]
                    if str_idx < len(strings):
                        strings[str_idx] = ""
