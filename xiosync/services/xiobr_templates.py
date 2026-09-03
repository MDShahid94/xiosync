"""Backward-compat shim — register_xiobr_templates moved to xiosync.subsystems.xiogrid.templates."""
from xiosync.subsystems.xiogrid.templates import *  # noqa: F401, F403
from xiosync.subsystems.xiogrid.templates import register_xiobr_templates  # noqa: F401
