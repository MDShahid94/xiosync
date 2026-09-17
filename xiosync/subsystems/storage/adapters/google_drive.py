"""Google Drive storage adapter.

Returns access instructions for workers to read/write objects in a
Google Drive folder using the Drive REST API.

Config keys:
    folder_id         (required) Google Drive folder ID for blob storage
    shared_drive_id   (optional) If blobs live in a Shared Drive
    worker_mount_path (optional) If the worker has Drive mounted locally,
                      set this to the mount root (e.g. '/content/drive/MyDrive'
                      for Colab, '/mnt/drive' for a VM). When set, the adapter
                      includes a local_path hint in metadata. If not set,
                      workers must use the Drive REST API exclusively.

Credential (vault):
    Service account JSON key (base64-encoded) OR OAuth refresh token.
    If not set, workers use their own runtime credentials.
"""
from __future__ import annotations

import json
from typing import Any

from xiosync.subsystems.storage.adapters.base import StorageAdapter, AccessInfo


_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
_DRIVE_FILES_URL  = "https://www.googleapis.com/drive/v3/files"


class GoogleDriveAdapter(StorageAdapter):

    def validate_config(self) -> None:
        if "folder_id" not in self.config:
            raise ValueError("google_drive provider requires config.folder_id")

    def get_access_info(self, object_key: str, operation: str) -> AccessInfo:
        """Return Drive access instructions for the worker.

        For upload:   Worker POSTs multipart to the Drive upload endpoint.
        For download: Worker GETs via Drive files.get?alt=media.
        For delete:   Worker DELETEs via Drive files.delete.

        If config.worker_mount_path is set, the metadata also includes a
        local_path hint so workers with a mounted filesystem can skip the
        REST API entirely.
        """
        folder_id       = self.config["folder_id"]
        shared_drive_id = self.config.get("shared_drive_id")
        mount_path      = self.config.get("worker_mount_path")  # None if not configured

        headers: dict[str, str] = {}
        if self.credential:
            try:
                _sa = json.loads(self.credential)
                # Service account — worker must exchange for an access token
                headers["X-SA-Credential"] = "present"
            except (json.JSONDecodeError, ValueError):
                # Raw OAuth access token
                headers["Authorization"] = f"Bearer {self.credential}"

        meta: dict[str, Any] = {
            "folder_id":  folder_id,
            "object_key": object_key,
        }
        if shared_drive_id:
            meta["shared_drive_id"] = shared_drive_id
        if mount_path:
            # Optional convenience hint for workers with Drive mounted locally
            meta["local_path"] = f"{mount_path.rstrip('/')}/{object_key}"

        if operation == "upload":
            return AccessInfo(
                provider_type="google_drive",
                operation="upload",
                url=f"{_DRIVE_UPLOAD_URL}?uploadType=multipart",
                headers=headers,
                method="POST",
                metadata=meta,
            )
        elif operation == "download":
            # Worker resolves object_key → Drive file ID via search, then downloads
            q = f"name='{object_key.split('/')[-1]}' and '{folder_id}' in parents and trashed=false"
            return AccessInfo(
                provider_type="google_drive",
                operation="download",
                url=f"{_DRIVE_FILES_URL}?q={q}&fields=files(id,name,size)",
                headers=headers,
                method="GET",
                metadata=meta,
            )
        else:  # delete
            return AccessInfo(
                provider_type="google_drive",
                operation="delete",
                url=_DRIVE_FILES_URL,  # Worker resolves file_id first, then DELETEs
                headers=headers,
                method="DELETE",
                metadata=meta,
            )

    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        """List objects in the Drive folder matching prefix.

        Requires a credential with Drive read access. Workers without
        stored credentials should query the Drive API directly.
        """
        # Full listing requires an active OAuth token — not available server-side
        # without a service account credential. Workers should call the Drive API
        # directly using the access info from get_access_info().\
        raise NotImplementedError(
            "google_drive list_objects requires a worker with Drive credentials. "
            "Use GET /storage/providers/{id}/access?operation=download to get access info "
            "and perform the listing from the worker."
        )

    def put(self, object_key: str, data: bytes) -> None:
        """Write ``data`` to Drive under ``object_key``.

        Strategy (tried in order):
        1. **FUSE mount**: If ``config.worker_mount_path`` is set and the path
           exists on this machine, write via the filesystem — fast and atomic.
        2. **REST API**: Upload using Drive Multipart Upload with the stored
           service account or OAuth credential. Requires ``requests`` package.

        Raises ``RuntimeError`` if neither strategy is available.
        """
        import os  # noqa: PLC0415

        # ── Strategy 1: FUSE mount ─────────────────────────────────────────
        mount_path = self.config.get("worker_mount_path", "")
        if mount_path:
            from pathlib import Path  # noqa: PLC0415
            full_path = Path(mount_path.rstrip("/")) / object_key
            full_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic-ish: write to tmp alongside target, then rename
            tmp_path = full_path.with_suffix(full_path.suffix + ".tmp")
            try:
                tmp_path.write_bytes(data)
                tmp_path.replace(full_path)
                return
            except OSError:
                # FUSE mount may be read-only or not actually present — fall through
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        # ── Strategy 2: Drive REST API multipart upload ────────────────────
        if not self.credential:
            raise RuntimeError(
                "google_drive adapter: no FUSE mount path and no credential configured. "
                "Cannot put object without one of these."
            )

        try:
            import requests  # noqa: PLC0415
        except ImportError:
            raise RuntimeError(
                "google_drive adapter: 'requests' package required for REST upload. "
                "pip install requests"
            )

        # Resolve access token
        access_token: str
        try:
            sa_info = json.loads(self.credential)
            # Service account — exchange for access token via JWT
            import time, base64, hashlib, hmac  # noqa: PLC0415, E401
            try:
                import google.auth.transport.requests as _gtr  # noqa: PLC0415
                import google.oauth2.service_account as _gsa   # noqa: PLC0415
                creds = _gsa.Credentials.from_service_account_info(
                    sa_info,
                    scopes=["https://www.googleapis.com/auth/drive.file"],
                )
                creds.refresh(_gtr.Request())
                access_token = creds.token
            except ImportError:
                raise RuntimeError(
                    "google_drive adapter: 'google-auth' package required for service account uploads. "
                    "pip install google-auth"
                )
        except (json.JSONDecodeError, ValueError):
            # Treat credential as a raw OAuth access token
            access_token = self.credential

        folder_id = self.config["folder_id"]
        file_name  = object_key.split("/")[-1]

        # Check for existing file with this name (to update instead of create)
        q = f"name='{file_name}' and '{folder_id}' in parents and trashed=false"
        list_resp = requests.get(
            _DRIVE_FILES_URL,
            params={"q": q, "fields": "files(id)", "spaces": "drive"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        existing_files = list_resp.json().get("files", []) if list_resp.ok else []

        import io  # noqa: PLC0415
        if existing_files:
            # PATCH existing file content
            file_id = existing_files[0]["id"]
            resp = requests.patch(
                f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media",
                headers={
                    "Authorization":  f"Bearer {access_token}",
                    "Content-Type":   "application/octet-stream",
                },
                data=data,
                timeout=60,
            )
        else:
            # POST new file with multipart metadata + content
            import mimetypes  # noqa: PLC0415
            meta = json.dumps({"name": file_name, "parents": [folder_id]}).encode()
            boundary = b"xiosync_boundary_" + os.urandom(8).hex().encode()
            body = (
                b"--" + boundary + b"\r\n"
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n" +
                meta + b"\r\n"
                b"--" + boundary + b"\r\n"
                b"Content-Type: application/octet-stream\r\n\r\n" +
                data + b"\r\n"
                b"--" + boundary + b"--"
            )
            resp = requests.post(
                f"{_DRIVE_UPLOAD_URL}?uploadType=multipart",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type":  f"multipart/related; boundary={boundary.decode()}",
                },
                data=body,
                timeout=60,
            )

        if not resp.ok:
            raise RuntimeError(
                f"google_drive adapter: Drive API upload failed "
                f"({resp.status_code}): {resp.text[:300]}"
            )

