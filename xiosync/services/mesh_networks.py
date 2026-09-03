"""Backward-compat shim — MeshNetworkService moved to xiosync.subsystems.xiogrid."""
from xiosync.subsystems.xiogrid.services.mesh_networks import *  # noqa: F401, F403
from xiosync.subsystems.xiogrid.services.mesh_networks import (  # noqa: F401
    MeshNetworkService,
    MeshNetworkRecord,
)
