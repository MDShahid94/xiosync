"""Storage adapter base — interface every provider adapter must implement.

Adapters handle the actual I/O with the provider (Drive, R2, S3, local).
XIOSYNC does NOT proxy large blobs in-process — instead:

  1. Worker calls GET /storage/providers/{id}/access-info → adapter returns
     upload/download instructions (Drive folder_id, R2 presigned URL, etc.)
  2. Worker performs the actual blob transfer directly to the provider.
  3. Worker calls POST /storage/objects to register the object in XIOSYNC's index.

This keeps XIOSYNC lean (no blob traffic through the API server) while still
providing a unified object index and provider abstraction.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AccessInfo:
    """Instructions a worker needs to access a blob directly.

    Returned by get_access_info() — the worker uses these to upload/download
    without routing through XIOSYNC.
    """
    provider_type: str          # 'google_drive' | 'cloudflare_r2' | 's3' | 'local'
    operation: str              # 'upload' | 'download' | 'delete'
    # Provider-specific fields — all optional, populated by adapter:
    url: str | None = None      # Presigned URL (R2/S3) or Drive API URL
    headers: dict[str, str] | None = None   # Auth headers if needed
    method: str = "PUT"         # HTTP method for presigned URL operations
    metadata: dict[str, Any] | None = None  # e.g. Drive folder_id, local path


class StorageAdapter(ABC):
    """Abstract base for all storage provider adapters."""

    def __init__(self, config: dict[str, Any], credential: str | None) -> None:
        """
        Args:
            config:     Provider config from storage_providers.config (non-secret).
            credential: Decrypted credential from vault (may be None for local/public).
        """
        self.config = config
        self.credential = credential

    @abstractmethod
    def get_access_info(self, object_key: str, operation: str) -> AccessInfo:
        """Return instructions for a worker to access a blob directly.

        Args:
            object_key: Logical key (path relative to provider root).
            operation:  'upload' | 'download' | 'delete'
        """

    @abstractmethod
    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        """List objects in the provider (for sync/audit).

        Returns list of {key, size_bytes, checksum, content_type}.
        """

    def validate_config(self) -> None:
        """Validate provider config at registration time. Override to add checks."""


def make_adapter(provider_type: str, config: dict[str, Any],
                 credential: str | None) -> StorageAdapter:
    """Factory — instantiate the correct adapter for provider_type."""
    from xiosync.subsystems.storage.adapters.google_drive import GoogleDriveAdapter
    from xiosync.subsystems.storage.adapters.cloudflare_r2 import CloudflareR2Adapter
    from xiosync.subsystems.storage.adapters.s3 import S3Adapter
    from xiosync.subsystems.storage.adapters.local import LocalAdapter

    _MAP = {
        "google_drive":      GoogleDriveAdapter,
        "cloudflare_r2":     CloudflareR2Adapter,
        "s3":                S3Adapter,
        "local":             LocalAdapter,
    }
    cls = _MAP.get(provider_type)
    if not cls:
        raise ValueError(f"Unknown storage provider type: {provider_type!r}")
    return cls(config, credential)
