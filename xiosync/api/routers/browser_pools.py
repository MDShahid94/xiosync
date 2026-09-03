"""Backward-compat shim — browser_pools router moved to xiosync.subsystems.xiogrid.api."""
from xiosync.subsystems.xiogrid.api.browser_pools import router  # noqa: F401

__all__ = ["router"]
