#!/usr/bin/env python3
"""migrate_r2_to_drive.py — One-time migration: R2 (xio-mesh) → Google Drive FUSE

Migrates only XIOSYNC-relevant objects (not XIOBR/Node artefacts):
  ✅  chrome_profiles/*.tar.gz              → Drive  chrome_profiles/
  ✅  ts_states/TS_*.state                  → Drive  ts_states/
  ✅  cache/python/stealth-pkgs-v1.*        → Drive  cache/python/
  ✅  cache/playwright/ms-playwright-v1.*   → Drive  cache/playwright/
  ❌  cache/node/*     (XIOBR Node bundles — skip)
  ❌  db/*             (XIOBR SQLite — skip)
  ❌  agy-credentials/ (unrelated — skip)
  ❌  appetize/        (APK store — skip)
  ❌  _test/           (test files — skip)

Safe to re-run — XIODriveFS.put() skips identical content (SHA-256 dedup).

Usage (on Colab master after Drive FUSE mounted):
    python3 migrate_r2_to_drive.py [--dry-run] [--prefix chrome_profiles/]
"""
from __future__ import annotations
import argparse, os, sys, time
from pathlib import Path

# ── CLI ────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--prefix", default="", help="Only migrate keys with this prefix")
parser.add_argument("--drive-root", default="")
args = parser.parse_args()

# ── Config ─────────────────────────────────────────────────────────────────────
R2_ENDPOINT   = "https://e63a13ef226ce4708d121b8d27fdc8a5.r2.cloudflarestorage.com"
R2_BUCKET     = "xio-mesh"
R2_ACCESS_KEY = os.environ.get("XIORUN_R2_ACCESS_KEY", "f1cdad9be4d1a529fd94ee358bbc40ff")
R2_SECRET_KEY = os.environ.get("XIORUN_R2_SECRET_KEY",
                               "859458c2da81e9167e0e03d5e040438b1766f582d097a391a3b3b8a89fdf41e6")

DRIVE_ROOT = (args.drive_root
              or os.environ.get("XIO_DRIVE_ROOT", "")
              or "/content/drive/MyDrive/XIOSYNC-Shared")

INCLUDE_PREFIXES = [
    "chrome_profiles/",
    "ts_states/",
    "cache/python/stealth-pkgs-v1",
    "cache/playwright/ms-playwright-v1",
]
SKIP_PREFIXES = ["cache/node/", "agy-credentials/", "appetize/", "_test/", "db/"]


def _should_migrate(key: str) -> bool:
    for sp in SKIP_PREFIXES:
        if key.startswith(sp):
            return False
    if args.prefix and not key.startswith(args.prefix):
        return False
    return any(key.startswith(ip) for ip in INCLUDE_PREFIXES)


def _p(msg: str, end: str = "\n") -> None:
    print(msg, end=end, flush=True)


def _human(b: int) -> str:
    if b >= 1024**3: return f"{b/1024**3:.1f} GB"
    if b >= 1024**2: return f"{b/1024**2:.1f} MB"
    if b >= 1024:    return f"{b/1024:.1f} KB"
    return f"{b} B"


# ── Verify Drive mount ─────────────────────────────────────────────────────────
_p("=" * 60)
_p("  XIOSYNC Migration: R2 → Google Drive FUSE")
_p("=" * 60)
_p(f"  Drive root : {DRIVE_ROOT}")
_p(f"  R2 bucket  : {R2_BUCKET}")
if args.dry_run:
    _p("  Mode       : DRY RUN")
_p("")

if not os.path.isdir(DRIVE_ROOT):
    _p(f"❌ Drive FUSE root not found: {DRIVE_ROOT}")
    sys.exit(1)
_p(f"✅ Drive FUSE root: {DRIVE_ROOT}")

# ── Load XIODriveFS ────────────────────────────────────────────────────────────
_XIO_FS = None
# XIODriveFS needs xiosync_base, worker_secret, node_name — read from env
# (boot.py exports these as env vars on the Colab runtime)
_xiosync_base   = os.environ.get("XIOSYNC_BASE", "")
_worker_secret  = os.environ.get("WORKER_SECRET", "")
_node_name      = os.environ.get("NODE_NAME", "xiogrid--default--master")

for _candidate in ["/tmp/xio_drive_fs.py",
                   "/content/xiosync-worker/colab/xio_drive_fs.py"]:
    if os.path.exists(_candidate):
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("xio_drive_fs", _candidate)
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        try:
            _XIO_FS = _mod.XIODriveFS(
                xiosync_base=_xiosync_base,
                worker_secret=_worker_secret,
                node_name=_node_name,
                drive_fs_root=DRIVE_ROOT,
            )
            _p(f"✅ XIODriveFS loaded (dedup enabled)")
        except Exception as _e:
            _p(f"ℹ️  XIODriveFS init skipped ({_e}) — using direct FUSE writes")
        break

if _XIO_FS is None:
    _p("ℹ️  XIODriveFS not available — writing directly to FUSE mount (no lock/dedup)")


def _write(key: str, data: bytes) -> bool:
    """Returns True=written, False=skipped (same content)."""
    if _XIO_FS is not None:
        return _XIO_FS.put(key, data, skip_if_same=True)
    dest = Path(DRIVE_ROOT) / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == len(data):
        return False
    dest.write_bytes(data)
    return True


# ── Connect R2 ─────────────────────────────────────────────────────────────────
_p("\nConnecting to R2…")
try:
    import boto3
    from botocore.config import Config as _BC
except ImportError:
    os.system("pip install -q boto3"); import boto3; from botocore.config import Config as _BC

r2 = boto3.client("s3", endpoint_url=R2_ENDPOINT,
                  aws_access_key_id=R2_ACCESS_KEY,
                  aws_secret_access_key=R2_SECRET_KEY,
                  config=_BC(signature_version="s3v4"), region_name="auto")

# ── List & filter ──────────────────────────────────────────────────────────────
_p(f"Listing R2 bucket '{R2_BUCKET}'…")
all_objs: list[dict] = []
for page in r2.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET):
    all_objs.extend(page.get("Contents", []))

to_migrate = [o for o in all_objs if _should_migrate(o["Key"])]
excluded   = len(all_objs) - len(to_migrate)
total_size = sum(o.get("Size", 0) for o in to_migrate)

_p(f"  {len(all_objs)} total objects, {len(to_migrate)} to migrate "
   f"({_human(total_size)}), {excluded} excluded")

if not to_migrate:
    _p("ℹ️  Nothing to migrate."); sys.exit(0)

_p("\nMigration plan:")
for o in to_migrate:
    _p(f"  {_human(o.get('Size',0)):>9}  {o['Key']}")

if args.dry_run:
    _p("\n[DRY RUN] No files written."); sys.exit(0)

# ── Run migration ──────────────────────────────────────────────────────────────
_p(f"\n{'=' * 60}\nMigrating {len(to_migrate)} objects…\n{'=' * 60}")

migrated = skipped = errors = 0
t_start = time.time()

for idx, obj in enumerate(to_migrate, 1):
    key   = obj["Key"]
    size  = obj.get("Size", 0)
    _p(f"[{idx:03d}/{len(to_migrate):03d}] {key}  ({_human(size)})", end=" … ")
    try:
        t0   = time.time()
        data = r2.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read()
        dl_t = time.time() - t0

        t1      = time.time()
        written = _write(key, data)
        wr_t    = time.time() - t1

        if written:
            _p(f"✅  dl={dl_t:.1f}s wr={wr_t:.1f}s")
            migrated += 1
        else:
            _p("⏭  unchanged")
            skipped += 1
    except Exception as e:
        _p(f"❌ {e}")
        errors += 1

elapsed = time.time() - t_start
_p(f"\n{'=' * 60}")
_p(f"Done in {elapsed:.0f}s — ✅ {migrated} copied  ⏭ {skipped} unchanged  ❌ {errors} errors")
_p(f"{'=' * 60}")
if errors:
    sys.exit(1)
_p("\n✅ Migration complete. Future boots will use Drive cache for pkgs + Chromium.")
