"""XIOVIEW API — backwards compatibility re-export.

The actual implementation has been decomposed into:
  - routes.py         — FastAPI router definitions
  - session_manager.py — browser session lifecycle
  - control.py         — input dispatch and coordinate scaling
  - screencast.py      — CDP screencast engine
  - viewer.py          — HTML viewer rendering
  - protocol.py        — shared types and constants
"""
from xiosync.subsystems.xioview.api.routes import public_router, router

__all__ = ["router", "public_router"]
