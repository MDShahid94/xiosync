"""Backward-compat shim — browser models moved to xiosync.subsystems.xiogrid.models.browser.

SQLAlchemy mapper classes must NOT be imported with wildcard (*) to avoid
double-registration. Only named re-exports.
"""

from xiosync.subsystems.xiogrid.models.browser import (
    BrowserPool,  # noqa: F401
    BrowserSession,  # noqa: F401
    ComputeRuntime,  # noqa: F401
    MeshNetwork,  # noqa: F401
    MeshNode,  # noqa: F401
    RuntimeNode,  # noqa: F401
)
from xiosync.subsystems.xiogrid.models.exit_node import (
    FingerprintProfile,  # noqa: F401
    PPPoEExitNode,  # noqa: F401
    PPPoEHost,  # noqa: F401
)

__all__ = [
    "BrowserPool",
    "BrowserSession",
    "ComputeRuntime",
    "MeshNetwork",
    "MeshNode",
    "RuntimeNode",
]
