"""profile_store.py — Chrome profile tarball store backed by Google Drive FUSE (XIODriveFS).

Chrome full userDataDir tarballs are the primary session persistence mechanism.
They include cookies, localStorage, IndexedDB, service workers, and all browser
state — far richer than the cookie-only JSON fallback.

Storage: Google Drive FUSE via XIODriveFS (org-zero-drive provider).
Credentials come from env vars set by boot.py (XIOSYNC_BASE, WORKER_SECRET,
NODE_NAME, XIO_DRIVE_ROOT).

Profile key path convention (compatible with migrated R2 data):
    chrome_profiles/PRFL-{serial:03d}_{slug}.tar.gz

Restore flow:
    1. Derive object key from credentials.storage_object_key (or slug fallback)
    2. Call XIODriveFS.get(key) → bytes (Drive FUSE read with distributed lock)
    3. Extract tar.gz to /tmp/xiorun_profiles/{identity}__{node_slug}/
    4. Trim cache dirs (Service Worker/CacheStorage, Cache, GPUCache, etc.)
    5. Return local path — used as --user-data-dir by Chromium

Save flow (on teardown):
    1. Trim cache dirs
    2. tar -czf to memory
    3. XIODriveFS.put(key, bytes, skip_if_same=True)  → SHA-256 dedup, no-op if unchanged
    4. Upsert storage_objects row (size_bytes, checksum, last_accessed_at)
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Dirs to strip before archiving and after extraction (same set as XIOBR)
TRIM_DIRS = [
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "ShaderCache",
    os.path.join("Service Worker", "CacheStorage"),
    os.path.join("Service Worker", "ScriptCache"),
    "BudgetDatabase",
    "Network Action Predictor",
    "heavy_ad_intervention_opt_out.db",
]

_LOCAL_PROFILES_BASE = Path(tempfile.gettempdir()) / "xiorun_profiles"
_LOCAL_PROFILES_BASE.mkdir(exist_ok=True)

# Module-level XIODriveFS singleton (lazy-initialised on first use)
_XIO_FS_INSTANCE = None


def _slug_from_identity(identity_id: str) -> str:
    return identity_id.replace("-", "")[:16]


def _local_extract_path(identity_id: str, node_name: str) -> Path:
    """Return the local extraction dir for a profile.
    Node-name suffix prevents multi-node conflicts.
    """
    slug = re.sub(r"[^a-zA-Z0-9]", "", _slug_from_identity(identity_id))
    node_slug = re.sub(r"[^a-zA-Z0-9-]", "-", node_name or "default")
    return _LOCAL_PROFILES_BASE / f"PRFL_{slug}__{node_slug}"


def _trim_profile(profile_dir: Path) -> None:
    """Remove stale cache dirs before archiving or after extraction."""
    for rel in TRIM_DIRS:
        full = profile_dir / rel
        if full.exists():
            shutil.rmtree(full, ignore_errors=True)


def _get_drive_fs():
    """Return (or create) the module-level XIODriveFS singleton.

    Reads connection details from env vars set by boot.py:
        XIOSYNC_BASE   — https://karmas-mac-mini.taildd8b9a.ts.net
        WORKER_SECRET  — worker org secret
        NODE_NAME      — e.g. xiogrid--default--master-001
        XIO_DRIVE_ROOT — Drive FUSE mount root (default: /content/drive/MyDrive/XIOSYNC-Shared)

    Raises RuntimeError if XIODriveFS cannot be imported or initialised.
    """
    global _XIO_FS_INSTANCE  # noqa: PLW0603
    if _XIO_FS_INSTANCE is not None:
        return _XIO_FS_INSTANCE

    xiosync_base  = os.environ.get("XIOSYNC_BASE", "").rstrip("/")
    worker_secret = os.environ.get("WORKER_SECRET", "")
    node_name     = os.environ.get("NODE_NAME", "xiorun-worker")
    drive_root    = os.environ.get(
        "XIO_DRIVE_ROOT", "/content/drive/MyDrive/XIOSYNC-Shared"
    )

    if not xiosync_base:
        raise RuntimeError(
            "profile_store: XIOSYNC_BASE env var not set — cannot initialise XIODriveFS"
        )

    # Try to import XIODriveFS from the location boot.py places it
    _candidates = [
        "/tmp/xio_drive_fs.py",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "colab", "xio_drive_fs.py"),
    ]
    mod = None
    for cand in _candidates:
        if os.path.exists(cand):
            import importlib.util as _ilu  # noqa: PLC0415
            _spec = _ilu.spec_from_file_location("xio_drive_fs", cand)
            mod   = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(mod)
            break

    if mod is None:
        # Last resort: try installed package
        try:
            import xio_drive_fs as mod  # type: ignore[no-redef]  # noqa: PLC0415
        except ImportError:
            raise RuntimeError(
                "profile_store: cannot find xio_drive_fs module. "
                "Ensure boot.py has run or xio_drive_fs.py is in /tmp/"
            )

    _XIO_FS_INSTANCE = mod.XIODriveFS(
        xiosync_base=xiosync_base,
        worker_secret=worker_secret,
        node_name=node_name,
        drive_fs_root=drive_root,
    )
    logger.info(
        "xiorun.profile_store.drive_fs_ready",
        extra={"drive_root": drive_root, "node": node_name},
    )
    return _XIO_FS_INSTANCE


def _normalize_username(identifier: str) -> str:
    """Derive a filesystem-safe lowercase username from an identity identifier.

    Mirrors XIOBR naming.py _normalize_username() for key compatibility:
      'user@gmail.com' → 'user'
      'karmareturnsfromallsides' → 'karmareturnsfromallsides'
      'PRFL-003_user' → 'user'  (strip existing prefix)
    """
    import re as _re  # noqa: PLC0415
    u = identifier.split("@")[0]
    u = _re.sub(r"^PRFL-\d+_", "", u, flags=_re.IGNORECASE)
    u = _re.sub(r"[^a-zA-Z0-9]", "_", u)
    u = _re.sub(r"_+", "_", u).strip("_")
    return u.lower()


def _canonical_profile_key(identity_id: str, engine: object) -> str:
    """Return the canonical Drive key for a Chrome profile tarball.

    Key format (matches XIOBR naming.py + XIOSYNC convention):
        chrome_profiles/PRFL-{serial:03d}_{username}.tar.gz

    serial comes from identities.serial — a permanent PostgreSQL SEQUENCE
    value that is assigned once at row creation and never changes on deletion.
    This guarantees stable file references even after rows are archived.

    Falls back to legacy hex-slug key if the serial column is not yet available
    (migration 0045 pending) so existing deployments are not broken.
    """
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

    with OrmSession(engine) as sess:
        row = sess.execute(
            text("""
                SELECT serial, identifier
                FROM identities
                WHERE id = :iid
                LIMIT 1
            """),
            {"iid": uuid.UUID(identity_id)},
        ).mappings().first()

        if row and row["serial"] is not None:
            username = _normalize_username(row["identifier"] or "")
            return f"chrome_profiles/PRFL-{int(row['serial']):03d}_{username}.tar.gz"

    # Fallback: hex-slug key (pre-0045 migration or identity not found)
    slug = _slug_from_identity(identity_id)
    return f"chrome_profiles/PRFL_{slug}.tar.gz"


def lookup_drive_object_key(identity_id: str, engine: object) -> str:
    """Find Drive object key for a Chrome profile tarball.

    Resolution order:
      1. credentials.storage_object_key (explicit override — highest priority)
      2. Canonical key from identities.serial + identifier
         → chrome_profiles/PRFL-{serial:03d}_{username}.tar.gz
      3. Legacy hex-slug fallback (pre-migration deployments)
    """
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

    with OrmSession(engine) as sess:
        # 1. Explicit override stored in credentials
        row = sess.execute(
            text("""
                SELECT c.storage_object_key
                FROM credentials c
                WHERE c.identity_id = :iid
                  AND c.credential_type = 'cookie_state'
                  AND c.storage_object_key IS NOT NULL
                LIMIT 1
            """),
            {"iid": uuid.UUID(identity_id)},
        ).mappings().first()

        if row and row["storage_object_key"]:
            return row["storage_object_key"]

    # 2 + 3. Canonical key (uses serial) or legacy slug fallback
    return _canonical_profile_key(identity_id, engine)


# Keep old name as alias so existing callers don't break
# Public canonical alias — Drive replaced Cloudflare R2 as the primary profile store.
lookup_drive_object_key = lookup_drive_object_key  # noqa: PLW0127 (explicit re-export)

# Backward-compat alias — deprecated; use lookup_drive_object_key instead.
def lookup_r2_object_key(identity_id: str, engine) -> str | None:  # type: ignore[return]
    """Deprecated: use lookup_drive_object_key. Will be removed in a future release."""
    import warnings  # noqa: PLC0415
    warnings.warn(
        "lookup_r2_object_key is deprecated; use lookup_drive_object_key instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return lookup_drive_object_key(identity_id, engine)


class ChromeProfileStore:
    """Google Drive FUSE-backed Chrome userDataDir tarball store."""

    def __init__(self, engine: object) -> None:
        self._engine = engine

    async def restore(
        self,
        identity_id: str,
        org_id: str,
        node_name: str,
    ) -> str | None:
        """Pull profile tar.gz from Drive FUSE and extract locally.

        Returns local path string (for --user-data-dir), or None if no profile
        exists in Drive (fresh session — blank Chromium profile).
        """
        import asyncio  # noqa: PLC0415

        object_key = lookup_drive_object_key(identity_id, self._engine)
        local_dir  = _local_extract_path(identity_id, node_name)

        # Warm pool reuse: already extracted on this runtime
        if local_dir.exists() and any(local_dir.iterdir()):
            logger.info("xiorun.profile.cache_hit", extra={
                "identity_id": identity_id, "local_dir": str(local_dir),
            })
            return str(local_dir)

        tar_bytes: bytes | None = await asyncio.get_event_loop().run_in_executor(
            None, self._download_sync, object_key
        )
        if tar_bytes is None:
            logger.info("xiorun.profile.not_in_drive", extra={
                "identity_id": identity_id, "key": object_key,
            })
            return None

        local_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(tar_bytes)
            tmp_path = tmp.name
        try:
            with tarfile.open(tmp_path, "r:gz") as tf:
                tf.extractall(path=str(local_dir.parent))
        finally:
            os.unlink(tmp_path)

        _trim_profile(local_dir)

        logger.info("xiorun.profile.restored", extra={
            "identity_id": identity_id, "key": object_key, "local_dir": str(local_dir),
        })
        return str(local_dir)

    async def save(
        self,
        identity_id: str,
        org_id: str,
        local_dir: str,
    ) -> None:
        """Tar and push profile to Drive FUSE. Fire-and-forget safe."""
        import asyncio  # noqa: PLC0415

        profile_path = Path(local_dir)
        if not profile_path.exists():
            logger.warning("xiorun.profile.save_no_dir", extra={
                "identity_id": identity_id, "local_dir": local_dir,
            })
            return

        await asyncio.get_event_loop().run_in_executor(
            None, self._save_sync, identity_id, org_id, profile_path
        )

    # ── Sync internals (run in executor) ─────────────────────────────────────

    def _download_sync(self, object_key: str) -> bytes | None:
        """Download profile tar.gz from Drive FUSE via XIODriveFS."""
        try:
            fs = _get_drive_fs()
            data = fs.get(object_key)
            if data is None:
                return None
            return data
        except Exception as exc:
            logger.warning("xiorun.profile.drive_download_error", extra={
                "key": object_key, "error": str(exc),
            })
            return None

    def _save_sync(self, identity_id: str, org_id: str, profile_path: Path) -> None:
        """Trim → tar → Drive FUSE upload → update storage_objects."""
        _trim_profile(profile_path)

        object_key = lookup_drive_object_key(identity_id, self._engine) or (
            f"chrome_profiles/PRFL_{_slug_from_identity(identity_id)}.tar.gz"
        )

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with tarfile.open(tmp_path, "w:gz") as tf:
                tf.add(str(profile_path), arcname=profile_path.name)
            with open(tmp_path, "rb") as f:
                tar_bytes = f.read()

            checksum = hashlib.sha256(tar_bytes).hexdigest()

            # Upload to Drive FUSE (skip_if_same=True → SHA-256 dedup, no write if unchanged)
            fs = _get_drive_fs()
            written = fs.put(object_key, tar_bytes, skip_if_same=True)

            self._upsert_storage_object(identity_id, object_key, len(tar_bytes), checksum)

            logger.info("xiorun.profile.saved", extra={
                "identity_id": identity_id,
                "key":         object_key,
                "size_bytes":  len(tar_bytes),
                "written":     written,  # False = content unchanged, Drive write skipped
            })
        finally:
            os.unlink(tmp_path)

    def _upsert_storage_object(
        self,
        identity_id: str,
        object_key: str,
        size_bytes: int,
        checksum: str,
    ) -> None:
        """Upsert storage_objects row for Drive provider tracking."""
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415
        from datetime import UTC, datetime  # noqa: PLC0415

        with OrmSession(self._engine) as sess:
            provider_id = sess.execute(
                text("SELECT id FROM storage_providers WHERE name='org-zero-drive' LIMIT 1")
            ).scalar()
            if not provider_id:
                logger.warning(
                    "xiorun.profile_store.no_drive_provider",
                    extra={"hint": "org-zero-drive row missing in storage_providers"},
                )
                return
            now = datetime.now(UTC)
            sess.execute(
                text("""
                    INSERT INTO storage_objects
                        (provider_id, object_key, object_type, size_bytes, checksum_sha256,
                         content_type, identity_id, last_accessed_at, created_at, updated_at)
                    VALUES
                        (:pid, :key, 'chrome_profile', :sz, :ck,
                         'application/gzip', :iid, :now, :now, :now)
                    ON CONFLICT (provider_id, object_key)
                    DO UPDATE SET
                        size_bytes       = EXCLUDED.size_bytes,
                        checksum_sha256  = EXCLUDED.checksum_sha256,
                        last_accessed_at = EXCLUDED.last_accessed_at,
                        updated_at       = EXCLUDED.updated_at
                """),
                {
                    "pid": provider_id,
                    "key": object_key,
                    "sz":  size_bytes,
                    "ck":  checksum,
                    "iid": uuid.UUID(identity_id),
                    "now": now,
                },
            )
            sess.commit()
