"""Chrome profile storage, Drive FUSE synchronization, and cache management."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
from typing import Any
import uuid

from fastapi import HTTPException
from pydantic import BaseModel

logger = logging.getLogger("xiorun_agent.storage.profile_manager")

DRIVE_ROOT = os.environ.get("XIO_DRIVE_ROOT", "/content/drive/MyDrive/XIOSYNC-Shared")
NODE_NAME = os.environ.get("NODE_NAME", os.environ.get("XIO_NODE_NAME", "colab-agent"))

PROFILES_BASE = Path(tempfile.gettempdir()) / "xiorun_profiles"
PROFILES_BASE.mkdir(exist_ok=True)

TRIM_DIRS = [
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "ShaderCache",
    os.path.join("Service Worker", "CacheStorage"),
    os.path.join("Service Worker", "ScriptCache"),
]


class PullProfileRequest(BaseModel):
    identity_id: str
    # Accept both names: drive_object_key (new canonical) and r2_object_key (deprecated)
    drive_object_key: str | None = None
    r2_object_key: str | None = None  # deprecated — use drive_object_key
    # If provided, extract profile to this exact dir (e.g. /tmp/uc-profile-SESSION_ID)
    # instead of PROFILES_BASE. Used by signin flow for profile reuse.
    target_dir: str | None = None

    @property
    def object_key(self) -> str:
        """Resolve whichever key name the caller used."""
        key = self.drive_object_key or self.r2_object_key or ""
        if not key:
            raise ValueError("Either drive_object_key or r2_object_key must be provided")
        return key


class PushProfileRequest(BaseModel):
    identity_id: str
    local_dir: str
    # Accept both names: drive_object_key (new canonical) and r2_object_key (deprecated)
    drive_object_key: str | None = None
    r2_object_key: str | None = None  # deprecated — use drive_object_key

    @property
    def object_key(self) -> str:
        """Resolve whichever key name the caller used."""
        key = self.drive_object_key or self.r2_object_key or ""
        if not key:
            raise ValueError("Either drive_object_key or r2_object_key must be provided")
        return key


# ── Drive FUSE helpers ─────────────────────────────────────────────────────────
def _drive_path(object_key: str, org_id: str = "", drive_root: str | None = None) -> Path:
    """Resolve an object key to its full Drive FUSE path.

    If org_id is provided and the key isn't already org-namespaced,
    the path is scoped under org_id/ to prevent cross-org access.
    """
    root = Path(drive_root or DRIVE_ROOT)
    if org_id and not object_key.startswith(f"org/{org_id}/") and not object_key.startswith("org/"):
        return root / "org" / org_id / object_key
    return root / object_key


drive_path = _drive_path


def _trim_profile(path: Path) -> None:
    """Trim redundant browser caches to save storage space and bandwidth."""
    for rel in TRIM_DIRS:
        full = path / rel
        if full.exists():
            shutil.rmtree(full, ignore_errors=True)


trim_profile = _trim_profile


async def pull_profile(req: PullProfileRequest, drive_root: str | None = None) -> dict[str, Any]:
    """Fetch Chrome profile tar.gz from Drive FUSE and extract locally.

    Returns local profile dir path, or {ok: False} if not found on Drive.
    If target_dir is provided, extracts directly there (for signin flow profile reuse).
    """
    if req.target_dir:
        local_dir = Path(req.target_dir)
    else:
        slug = req.identity_id.replace("-", "")[:16]
        local_dir = PROFILES_BASE / f"PRFL_{slug}__{NODE_NAME}"

    if local_dir.exists() and any(local_dir.iterdir()):
        logger.info(f"pull-profile cache-hit identity={req.identity_id} → {local_dir}")
        return {"ok": True, "profile_dir": str(local_dir), "source": "cache"}

    # Read from Drive FUSE
    drive_file = _drive_path(req.object_key, drive_root=drive_root)
    if not drive_file.exists():
        logger.info(f"pull-profile not-found identity={req.identity_id} key={req.object_key}")
        # Return ok=False (not 404) — signin flow treats missing profile as "fresh login"
        return {"ok": False, "profile_dir": None, "reason": "not_on_drive"}

    try:
        tar_bytes = drive_file.read_bytes()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Drive read failed: {exc}") from exc

    # Extract into a temp staging dir to avoid partial-write races
    stage_dir = PROFILES_BASE / f"_stage_{uuid.uuid4().hex[:8]}"
    stage_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(tar_bytes)
        tmp_path = tmp.name
    try:
        with tarfile.open(tmp_path, "r:gz") as tf:
            tf.extractall(path=str(stage_dir))
    finally:
        os.unlink(tmp_path)

    # The tarball root dir name may differ from local_dir — find it and rename
    extracted_children = [p for p in stage_dir.iterdir() if p.is_dir()]
    if not extracted_children:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Tarball extracted no directories")

    extracted_root = extracted_children[0]
    if local_dir.exists():
        shutil.rmtree(local_dir, ignore_errors=True)
    local_dir.mkdir(parents=True, exist_ok=True)
    # Move contents (not the root dir) if tarball wraps in a subdirectory
    shutil.move(str(extracted_root), str(local_dir))
    shutil.rmtree(stage_dir, ignore_errors=True)

    _trim_profile(local_dir)

    logger.info(f"pull-profile identity={req.identity_id} key={req.object_key} → {local_dir}")
    return {"ok": True, "profile_dir": str(local_dir), "source": "drive"}


async def push_profile(req: PushProfileRequest, drive_root: str | None = None) -> dict[str, Any]:
    """Tar local Chrome profile dir and write back to Drive FUSE."""
    profile_path = Path(req.local_dir)
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail=f"Profile dir not found: {req.local_dir}")

    _trim_profile(profile_path)

    # ── Cookie count validation ──────────────────────────────────
    _min_cookies = int(os.environ.get("XIOSYNC_MIN_COOKIE_COUNT", "30"))
    _cookies_db = profile_path / "Default" / "Cookies"
    if not _cookies_db.exists():
        _cookies_db = profile_path / "Default" / "Network" / "Cookies"
    if _cookies_db.exists():
        try:
            conn = sqlite3.connect(str(_cookies_db))
            cookie_count = conn.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
            conn.close()
            if cookie_count < _min_cookies:
                raise HTTPException(
                    status_code=400,
                    detail=f"Profile has only {cookie_count} cookies (minimum: {_min_cookies}). "
                    f"Profile may be degraded — refusing push.",
                )
            logger.info(f"Cookie count validation passed: {cookie_count} >= {_min_cookies}")
        except sqlite3.Error as e:
            logger.warning(f"Cookie DB read failed (non-fatal): {e}")

    drive_file = _drive_path(req.object_key, drive_root=drive_root)
    drive_file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with tarfile.open(tmp_path, "w:gz") as tf:
            tf.add(str(profile_path), arcname=profile_path.name)
        tar_bytes = Path(tmp_path).read_bytes()
        # Atomic-ish: write to tmp alongside target, then replace
        drive_tmp = drive_file.with_suffix(".tar.gz.tmp")
        drive_tmp.write_bytes(tar_bytes)
        drive_tmp.replace(drive_file)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    logger.info(
        f"push-profile identity={req.identity_id} key={req.object_key} size={len(tar_bytes)}"
    )
    return {"ok": True, "size_bytes": len(tar_bytes)}
