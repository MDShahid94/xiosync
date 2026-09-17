"""xio_drive_fs.py — XIODriveFS: Drive File Stream accessor with distributed locking.

Deployed by XIOSYNC. Fetched by boot.py at boot via:
  GET /api/v1/workers/xio-drive-fs.py

Architecture
============
* Google Drive is mounted via FUSE at /content/drive (google.colab.drive.mount).
* A shortcut in "My Drive" -> XIOSYNC shared folder makes it appear at a
  deterministic path inside the FUSE mount. All blob I/O uses plain OS file
  operations -- NO Drive REST API quota consumed for reads/writes.
* Distributed locking uses XIOSYNC's Redis-backed lock API (primary).
  Fallback: advisory .xiolock file on the FUSE mount itself.
* Deduplication: SHA-256 checksum comparison before any write.

Shortcut creation is the only REST API call (one-time per Google account).

Usage
=====
    from xio_drive_fs import XIODriveFS, mount_drive_and_ensure_shortcut

    root = mount_drive_and_ensure_shortcut(
        folder_id="19k79lkPzg1gBM7IhIhE-35rfiAyVfCsK",
        shortcut_name="XIOSYNC-Shared",
    )
    fs = XIODriveFS(
        xiosync_base="http://100.86.149.127:8001",
        worker_secret="...",
        node_name="colab-master",
        drive_fs_root=root,
    )
    written = fs.put("ts_states/TS_colab-master.state", data)
    data    = fs.get("ts_states/TS_colab-master.state")
    keys    = fs.list_prefix("ts_states/")
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path

logger = logging.getLogger("xio_drive_fs")

# ── Constants ─────────────────────────────────────────────────────────────────
_LOCK_SUFFIX = ".xiolock"   # advisory lock file extension
_TMP_SUFFIX  = ".xiotmp"    # atomic write staging extension
_LOCK_TTL_S  = 60           # seconds before a held lock is considered stale
_LOCK_WAIT_S = 45           # default timeout waiting to acquire a lock
_LOCK_POLL_S = 1.2          # polling interval base (+ random jitter)


class XIODriveFSError(Exception):
    """Base exception for XIODriveFS errors."""


class XIOLockTimeout(XIODriveFSError):
    """Lock could not be acquired within the specified timeout."""


class XIODriveFS:
    """Distributed-locked, deduplicated filesystem accessor over Drive FUSE mount."""

    def __init__(
        self,
        *,
        xiosync_base: str,
        worker_secret: str,
        node_name: str,
        drive_fs_root: str = "/content/drive/MyDrive/XIOSYNC-Shared",
        lock_ttl: int = _LOCK_TTL_S,
    ) -> None:
        self.xiosync_base  = xiosync_base.rstrip("/")
        self.worker_secret = worker_secret
        self.node_name     = node_name
        self.root          = Path(drive_fs_root)
        self.lock_ttl      = lock_ttl

    # ── Path helpers ──────────────────────────────────────────────────────────

    def _abspath(self, key: str) -> Path:
        resolved = (self.root / key).resolve()
        if not str(resolved).startswith(str(self.root.resolve())):
            raise ValueError(f"Key {key!r} escapes drive_fs_root")
        return resolved

    def _lockpath(self, key: str) -> Path:
        p = self._abspath(key)
        return p.parent / (p.name + _LOCK_SUFFIX)

    @staticmethod
    def checksum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # ── XIOSYNC Redis lock (primary) ──────────────────────────────────────────

    def _redis_acquire(self, key: str) -> bool | None:
        """Try to acquire lock via XIOSYNC Redis.
        True=acquired, False=held by another, None=server unreachable.
        """
        import urllib.request as _urq  # noqa: PLC0415
        try:
            body = json.dumps({
                "resource_key": key,
                "node_name":    self.node_name,
                "ttl_seconds":  self.lock_ttl,
            }).encode()
            req = _urq.Request(
                f"{self.xiosync_base}/api/v1/workers/lock/acquire",
                data=body,
                headers={"Content-Type": "application/json",
                         "X-Worker-Secret": self.worker_secret},
                method="POST",
            )
            resp = json.loads(_urq.urlopen(req, timeout=5).read())
            return bool(resp.get("acquired", False))
        except Exception as exc:
            logger.debug("XIOSYNC lock acquire unreachable: %s", exc)
            return None

    def _redis_release(self, key: str) -> None:
        """Release XIOSYNC Redis lock (best-effort, never raises)."""
        import urllib.request as _urq  # noqa: PLC0415
        try:
            body = json.dumps({"resource_key": key, "node_name": self.node_name}).encode()
            req = _urq.Request(
                f"{self.xiosync_base}/api/v1/workers/lock/release",
                data=body,
                headers={"Content-Type": "application/json",
                         "X-Worker-Secret": self.worker_secret},
                method="POST",
            )
            _urq.urlopen(req, timeout=5)
        except Exception as exc:
            logger.debug("XIOSYNC lock release failed (non-fatal): %s", exc)

    # ── Filesystem advisory lock (fallback) ───────────────────────────────────

    def _fs_acquire(self, key: str, deadline: float) -> bool:
        lpath = self._lockpath(key)
        lpath.parent.mkdir(parents=True, exist_ok=True)

        while time.monotonic() < deadline:
            if lpath.exists():
                try:
                    data = json.loads(lpath.read_text())
                    if time.time() - data.get("acquired_at", 0) > self.lock_ttl:
                        lpath.unlink(missing_ok=True)
                except (json.JSONDecodeError, OSError):
                    lpath.unlink(missing_ok=True)

            if not lpath.exists():
                try:
                    with open(lpath, "x") as f:
                        json.dump({"node": self.node_name, "acquired_at": time.time(),
                                   "ttl": self.lock_ttl}, f)
                    return True
                except FileExistsError:
                    pass

            time.sleep(_LOCK_POLL_S + random.uniform(0.0, 0.5))
        return False

    def _fs_release(self, key: str) -> None:
        try:
            lpath = self._lockpath(key)
            if lpath.exists():
                try:
                    if json.loads(lpath.read_text()).get("node") == self.node_name:
                        lpath.unlink(missing_ok=True)
                except (json.JSONDecodeError, OSError):
                    lpath.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("FS lock release failed (non-fatal): %s", exc)

    # ── Unified lock interface ────────────────────────────────────────────────

    def acquire_lock(self, key: str, timeout: int = _LOCK_WAIT_S) -> str:
        """Acquire distributed lock for key.

        Tries XIOSYNC Redis first. Falls back to .xiolock file if server
        is unreachable. Raises XIOLockTimeout after timeout seconds.

        Returns lock mode string: 'redis' or 'fs'.
        """
        deadline = time.monotonic() + timeout
        use_fs   = False

        while time.monotonic() < deadline:
            if use_fs:
                if self._fs_acquire(key, deadline):
                    return "fs"
                raise XIOLockTimeout(f"Filesystem lock timed out for {key!r}")

            result = self._redis_acquire(key)
            if result is True:
                return "redis"
            if result is None:
                logger.warning("XIOSYNC lock server unreachable -- falling back to .xiolock for %r", key)
                use_fs = True
                continue
            # False = held by another node; wait and retry
            time.sleep(_LOCK_POLL_S + random.uniform(0.0, 0.5))

        raise XIOLockTimeout(f"Timed out waiting for lock on {key!r} after {timeout}s")

    def release_lock(self, key: str, mode: str) -> None:
        if mode == "redis":
            self._redis_release(key)
        else:
            self._fs_release(key)

    # ── Core I/O ─────────────────────────────────────────────────────────────

    def exists(self, key: str) -> bool:
        """True if key exists on Drive FUSE mount. No lock needed."""
        return self._abspath(key).exists()

    def put(
        self,
        key: str,
        data: bytes,
        *,
        skip_if_same: bool = True,
        lock_timeout: int = _LOCK_WAIT_S,
    ) -> bool:
        """Write data to key on Drive FUSE mount.

        Returns True if written, False if skipped (identical content).
        Uses atomic tmp+rename to prevent partial writes.
        """
        dest      = self._abspath(key)
        new_cksum = self.checksum(data)

        # Fast-path dedup (no lock needed)
        if skip_if_same and dest.exists():
            try:
                if self.checksum(dest.read_bytes()) == new_cksum:
                    logger.debug("put(%r) skipped -- same content (fast path)", key)
                    return False
            except OSError:
                pass

        dest.parent.mkdir(parents=True, exist_ok=True)
        mode = self.acquire_lock(key, timeout=lock_timeout)
        try:
            # Re-check inside lock (another worker may have written while waiting)
            if skip_if_same and dest.exists():
                try:
                    if self.checksum(dest.read_bytes()) == new_cksum:
                        logger.debug("put(%r) skipped -- same content (inside lock)", key)
                        return False
                except OSError:
                    pass

            # Atomic write
            tmp = dest.with_suffix(dest.suffix + _TMP_SUFFIX)
            tmp.write_bytes(data)
            tmp.rename(dest)
            logger.debug("put(%r) wrote %d bytes [lock=%s]", key, len(data), mode)
            return True
        finally:
            self.release_lock(key, mode)

    def get(
        self,
        key: str,
        *,
        lock: bool = True,
        lock_timeout: int = 15,
    ) -> bytes | None:
        """Read data from key. Returns None if key does not exist."""
        dest = self._abspath(key)
        if not dest.exists():
            return None
        if not lock:
            return dest.read_bytes()
        mode = self.acquire_lock(key, timeout=lock_timeout)
        try:
            return dest.read_bytes() if dest.exists() else None
        finally:
            self.release_lock(key, mode)

    def delete(self, key: str, *, lock_timeout: int = _LOCK_WAIT_S) -> bool:
        """Delete key. Returns True if deleted, False if it didn't exist."""
        dest = self._abspath(key)
        if not dest.exists():
            return False
        mode = self.acquire_lock(key, timeout=lock_timeout)
        try:
            if dest.exists():
                dest.unlink()
                return True
            return False
        finally:
            self.release_lock(key, mode)

    def list_prefix(self, prefix: str = "") -> list[str]:
        """List all keys under prefix. No lock -- snapshot only.
        Excludes internal .xiolock and .xiotmp files.
        """
        base = self._abspath(prefix) if prefix else self.root
        if not base.exists():
            return []
        if base.is_file():
            return [prefix]
        return sorted(
            str(p.relative_to(self.root))
            for p in base.rglob("*")
            if p.is_file()
            and not p.name.endswith(_LOCK_SUFFIX)
            and not p.name.endswith(_TMP_SUFFIX)
        )


# ── Drive mount helper (called from boot.py) ──────────────────────────────────

def mount_drive_and_ensure_shortcut(
    *,
    folder_id: str,
    shortcut_name: str = "XIOSYNC-Shared",
    mount_point: str = "/content/drive",
) -> str | None:
    """Mount Google Drive via Colab FUSE and ensure the XIOSYNC shortcut exists.

    Idempotent -- safe to call on every boot:
      - drive.mount() is a no-op if already mounted.
      - Shortcut creation only fires if it doesn't exist (1 LIST + maybe 1 CREATE).

    Returns the absolute FUSE path to the shared folder root, or None on error.

    Quota impact:
      First run: 1x LIST + 1x CREATE  (one-time per Google account)
      Every boot: 1x LIST only
      All blob I/O: ZERO Drive API quota -- pure FUSE filesystem operations
    """
    try:
        from google.colab import auth as _auth, drive as _drive  # noqa: PLC0415
        import google.auth as _gauth                             # noqa: PLC0415
        from googleapiclient.discovery import build as _build   # noqa: PLC0415

        _auth.authenticate_user()
        _drive.mount(mount_point, force_remount=False)
        print(f"  ✅ Drive mounted at {mount_point}", flush=True)

        _creds, _ = _gauth.default()
        _svc = _build("drive", "v3", credentials=_creds)

        _existing = _svc.files().list(
            q=(f"name='{shortcut_name}' "
               f"and mimeType='application/vnd.google-apps.shortcut' "
               f"and trashed=false"),
            fields="files(id,name)",
            spaces="drive",
        ).execute().get("files", [])

        if not _existing:
            _svc.files().create(
                body={
                    "name":     shortcut_name,
                    "mimeType": "application/vnd.google-apps.shortcut",
                    "shortcutDetails": {"targetId": folder_id},
                    "parents":  ["root"],
                },
                fields="id,name",
            ).execute()
            print(f"  ✅ Shortcut '{shortcut_name}' created in My Drive", flush=True)
        else:
            print(f"  ✅ Shortcut '{shortcut_name}' ready in My Drive", flush=True)

        fs_root = os.path.join(mount_point, "MyDrive", shortcut_name)
        os.makedirs(fs_root, exist_ok=True)
        return fs_root

    except ImportError:
        print("  ℹ️  google.colab unavailable -- Drive FUSE mount skipped", flush=True)
        return None
    except Exception as exc:
        print(f"  ⚠️  Drive mount failed ({type(exc).__name__}: {exc}) -- non-fatal", flush=True)
        return None
