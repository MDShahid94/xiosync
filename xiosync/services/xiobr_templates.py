"""Backward-compat shim — register_platform_templates (was register_xiobr_templates).

Import from xiosync.subsystems.xiogrid.templates directly.
"""
from xiosync.subsystems.xiogrid.templates import register_platform_templates as register_xiobr_templates  # noqa: F401
