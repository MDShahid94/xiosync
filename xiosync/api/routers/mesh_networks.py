"""Backward-compat shim — mesh_networks router moved to xiosync.subsystems.xiogrid.api."""
from xiosync.subsystems.xiogrid.api.mesh_networks import router  # noqa: F401

__all__ = ["router"]
