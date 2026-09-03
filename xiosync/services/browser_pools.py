"""Backward-compat shim — BrowserPoolService moved to xiosync.subsystems.xiogrid."""
from xiosync.subsystems.xiogrid.services.browser_pools import *  # noqa: F401, F403
from xiosync.subsystems.xiogrid.services.browser_pools import (  # noqa: F401
    BrowserPoolService,
    BrowserPoolNotFoundError,
    BrowserPoolRecord,
)
