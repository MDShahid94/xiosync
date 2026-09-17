"""Backward-compat shim — register_platform_types (was register_xiobr_types).

Import from xiosync.subsystems.xiogrid.bootstrap directly.
"""
from xiosync.subsystems.xiogrid.bootstrap import register_platform_types as register_xiobr_types  # noqa: F401
