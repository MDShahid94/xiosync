"""XIOGRID — Compute Orchestration Subsystem for XIOSYNC.

Registers all XIOGRID routers, models, and bootstrap hooks with the
XIOSYNC core platform. Import this module to activate XIOGRID.
"""
from xiosync.subsystems.xiogrid import models  # noqa: F401 - triggers model registration
from xiosync.subsystems.xiogrid.bootstrap import register_xiogrid  # noqa: F401

__version__ = "0.1.0"
__subsystem__ = "xiogrid"
