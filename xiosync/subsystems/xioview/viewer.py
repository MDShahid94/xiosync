"""XIOVIEW Viewer — serves the self-contained browser observation HTML page.

Loads the viewer template from static/viewer.html and performs variable
substitution for session ID and WebSocket URL. The HTML is cached after
first load for zero file I/O on subsequent requests.
"""

from __future__ import annotations

import logging
import pathlib
from functools import lru_cache

logger = logging.getLogger(__name__)

_STATIC_DIR = pathlib.Path(__file__).parent / "static"


@lru_cache(maxsize=1)
def _load_template() -> str:
    """Load and cache the viewer HTML template from disk."""
    template_path = _STATIC_DIR / "viewer.html"
    if not template_path.exists():
        raise FileNotFoundError(
            f"XIOVIEW viewer template not found at {template_path}. "
            "Ensure xiosync/subsystems/xioview/static/viewer.html exists."
        )
    text = template_path.read_text(encoding="utf-8")
    logger.info(
        "xioview.viewer_template_loaded",
        extra={
            "path": str(template_path),
            "size_bytes": len(text),
        },
    )
    return text


def render_viewer(
    session_id: str,
    ws_url: str,
    novnc_url: str | None = None,
    profile_id: str | None = None,
    hitl_notice: dict | None = None,
) -> str:
    """Render the XIOVIEW viewer HTML with session-specific values.

    Args:
        session_id: The browser session identifier.
        ws_url: Full WebSocket URL (ws:// or wss://) for the observe endpoint.
        novnc_url: Optional direct noVNC URL for low-latency HITL interaction.
        profile_id: Optional profile identifier (e.g. PRFL-002) for display.
        hitl_notice: Optional HITL notice dict with id, message, challenge_type, state.

    Returns:
        Complete HTML string ready to serve.
    """
    template = _load_template()
    import json as _json

    result = (
        template.replace("{{SESSION_ID}}", session_id)
        .replace("{{WS_URL}}", ws_url)
        .replace("{{NOVNC_URL}}", novnc_url or "")
        .replace("{{PROFILE_ID}}", profile_id or "")
        .replace("{{HITL_NOTICE_JSON}}", _json.dumps(hitl_notice) if hitl_notice else "null")
    )
    return result
