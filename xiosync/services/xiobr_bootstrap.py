"""Backward-compat shim — register_xiobr_types moved to xiosync.subsystems.xiogrid.bootstrap."""
from xiosync.subsystems.xiogrid.bootstrap import *  # noqa: F401, F403
from xiosync.subsystems.xiogrid.bootstrap import (  # noqa: F401
    _EVENT_TYPES,
    register_xiobr_types,
    register_xiogrid,
)
