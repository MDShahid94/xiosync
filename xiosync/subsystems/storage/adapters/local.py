"""Local filesystem storage adapter (Mac Mini, VM, self-hosted server).

Useful for: development, Mac Mini local blob cache, on-premise deployments
where no external cloud storage is configured.

Config keys:
    base_path  (required) Absolute path to the local blob directory
               e.g. '/var/lib/xiosync/blobs'

Credential: None (no auth for local filesystem).

The API server itself handles upload/download for local storage since the
worker and server share a filesystem (or NFS mount). Access info returns
a file:// URL that the server resolves.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from xiosync.subsystems.storage.adapters.base import StorageAdapter, AccessInfo


class LocalAdapter(StorageAdapter):

    def validate_config(self) -> None:
        if "base_path" not in self.config:
            raise ValueError("local provider requires config.base_path")

    def _full_path(self, object_key: str) -> Path:
        base = Path(self.config["base_path"])
        # Sanitize: prevent path traversal
        resolved = (base / object_key).resolve()
        if not str(resolved).startswith(str(base.resolve())):
            raise ValueError(f"Path traversal attempt blocked: {object_key!r}")
        return resolved

    def get_access_info(self, object_key: str, operation: str) -> AccessInfo:
        full_path = self._full_path(object_key)
        if operation == "upload":
            full_path.parent.mkdir(parents=True, exist_ok=True)
        return AccessInfo(
            provider_type="local",
            operation=operation,
            url=f"file://{full_path}",
            method="FILE",
            metadata={
                "base_path": self.config["base_path"],
                "full_path": str(full_path),
                "object_key": object_key,
            },
        )

    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        base = Path(self.config["base_path"])
        if not base.exists():
            return []
        results = []
        for path in base.rglob("*"):
            if path.is_file():
                rel_key = str(path.relative_to(base))
                if prefix and not rel_key.startswith(prefix):
                    continue
                stat = path.stat()
                results.append({
                    "key": rel_key,
                    "size_bytes": stat.st_size,
                    "content_type": None,
                    "checksum": None,
                })
        return results
