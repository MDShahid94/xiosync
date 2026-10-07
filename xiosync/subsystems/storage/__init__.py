"""xiosync.subsystems.storage"""

from xiosync.subsystems.storage.adapters import AccessInfo, make_adapter
from xiosync.subsystems.storage.service import (
    ObjectRecord,
    ProviderRecord,
    StorageNotFoundError,
    StorageProviderError,
    StorageService,
)

__all__ = [
    "StorageService",
    "ProviderRecord",
    "ObjectRecord",
    "StorageNotFoundError",
    "StorageProviderError",
    "make_adapter",
    "AccessInfo",
]
