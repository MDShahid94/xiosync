"""xiosync.subsystems.storage.adapters"""

from xiosync.subsystems.storage.adapters.base import AccessInfo, StorageAdapter, make_adapter
from xiosync.subsystems.storage.adapters.cloudflare_r2 import CloudflareR2Adapter
from xiosync.subsystems.storage.adapters.google_drive import GoogleDriveAdapter
from xiosync.subsystems.storage.adapters.local import LocalAdapter
from xiosync.subsystems.storage.adapters.s3 import S3Adapter

__all__ = [
    "StorageAdapter",
    "AccessInfo",
    "make_adapter",
    "GoogleDriveAdapter",
    "CloudflareR2Adapter",
    "S3Adapter",
    "LocalAdapter",
]
