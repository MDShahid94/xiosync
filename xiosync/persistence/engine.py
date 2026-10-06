"""xiosync.persistence.engine — Application DB engine accessor.

Bridges to the application-wide engine reference singleton defined in
xiosync.platform.engine_ref (RULE-ARCH-1 safe).
"""
from __future__ import annotations

from xiosync.platform.engine_ref import get_engine, set_engine

__all__ = ["get_engine", "set_engine"]
