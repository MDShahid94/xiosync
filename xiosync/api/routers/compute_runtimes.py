"""Backward-compat shim — compute_runtimes router moved to xiosync.subsystems.xiogrid.api."""
from xiosync.subsystems.xiogrid.api.compute_runtimes import router  # noqa: F401

__all__ = ["router"]
