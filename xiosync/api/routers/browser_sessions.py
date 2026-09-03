"""Backward-compat shim — browser_sessions router moved to xiosync.subsystems.xiogrid.api."""
from xiosync.subsystems.xiogrid.api.browser_sessions import router  # noqa: F401

__all__ = ["router"]
