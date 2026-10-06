#!/usr/bin/env python3
"""rename_ts_states.py — One-time Tailscale state file rename + DB sync.

Renames TS state files in Google Drive from legacy XIOBR naming to the
canonical XIOSYNC format:  ts_states/TS_{node_identity}.state

Handles three source formats:
  1. XIOBR legacy (no prefix): ts_states/{node_name}.state
                                e.g. ts_states/xiobr--default--worker.state
  2. XIOBR flat root:          {node_name}.state  (at drive root, not in ts_states/)
  3. Already canonical:        ts_states/TS_{node_name}.state  (skipped)

After rename, updates storage_objects.object_key in the DB so the next boot
lookup hits the fast path (Drive FUSE _XIO_FS.get("ts_states/TS_...") returns
the bytes immediately without a fresh Tailscale registration).

Node-name prefix mapping (XIOBR → XIOSYNC):
  xiobr--   → xiosync--
  xiogrid-- → xiosync--
  colab-    → xiosync--default--

Usage:
    python3 rename_ts_states.py [--dry-run] [--db-url <postgres_dsn>]
                                [--drive-root <path>]

Run this ONCE in Colab with Drive FUSE mounted at /content/drive.
Safe to re-run: canonical files are skipped, DB is idempotent (ON CONFLICT DO UPDATE).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# ── CLI ────────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser(description="Rename XIOBR TS state files to XIOSYNC canonical format")
ap.add_argument("--dry-run",    action="store_true", help="Print actions without executing")
ap.add_argument("--drive-root", default=os.environ.get("XIO_DRIVE_ROOT",
                "/content/drive/MyDrive/XIOSYNC-Shared"),
                help="Path to XIOSYNC-Shared Drive folder (default: /content/drive/MyDrive/XIOSYNC-Shared)")
ap.add_argument("--db-url",     default=os.environ.get("DATABASE_URL", ""),
                help="PostgreSQL DSN — needed to update storage_objects.object_key in DB")
ap.add_argument("--no-db",      action="store_true", help="Skip DB update (Drive rename only)")
args = ap.parse_args()

DRY        = args.dry_run
DRIVE_ROOT = Path(args.drive_root)
DB_URL     = args.db_url
NO_DB      = args.no_db

TS_DIR = DRIVE_ROOT / "ts_states"

def _p(*a): print(*a, flush=True)


# ── Node-name prefix normalisation ────────────────────────────────────────────

_PREFIX_MAP = [
    # (pattern, replacement)  — applied left to right, first match wins
    (re.compile(r"^xiobr--"),    "xiosync--"),
    (re.compile(r"^xiogrid--"),  "xiosync--"),
    (re.compile(r"^colab-master$"), "xiosync--default--worker"),
    (re.compile(r"^colab-"),     "xiosync--default--"),
]

def _normalise_node_name(raw: str) -> str:
    """Map legacy XIOBR node name to canonical XIOSYNC node name."""
    for pattern, replacement in _PREFIX_MAP:
        if pattern.search(raw):
            return pattern.sub(replacement, raw)
    return raw  # already canonical (starts with xiosync-- or unknown)


# ── Gather candidate files ─────────────────────────────────────────────────────

def _gather() -> list[tuple[Path, str]]:
    """Return list of (file_path, canonical_key) for files that need renaming."""
    candidates: list[tuple[Path, str]] = []

    # Source 1: ts_states/ subfolder — files without TS_ prefix
    if TS_DIR.exists():
        for f in sorted(TS_DIR.iterdir()):
            if not f.is_file() or not f.name.endswith(".state"):
                continue
            if f.name.startswith("TS_"):
                _p(f"  ✓ Already canonical: {f.relative_to(DRIVE_ROOT)}")
                continue
            # Strip .state suffix to get bare node name
            bare = f.stem  # e.g. "xiobr--default--worker"
            canonical_node = _normalise_node_name(bare)
            canonical_key  = f"ts_states/TS_{canonical_node}.state"
            candidates.append((f, canonical_key))
    else:
        _p(f"  ⚠️  ts_states/ dir not found at {TS_DIR} — will scan root only")

    # Source 2: Drive root — any *.state file that looks like a TS state
    for f in sorted(DRIVE_ROOT.iterdir()):
        if not f.is_file() or not f.name.endswith(".state"):
            continue
        bare = f.stem
        canonical_node = _normalise_node_name(bare)
        canonical_key  = f"ts_states/TS_{canonical_node}.state"
        candidates.append((f, canonical_key))

    return candidates


# ── DB update helper ───────────────────────────────────────────────────────────

def _db_update(renames: list[tuple[str, str]]) -> None:
    """Update storage_objects rows: old_key → new_key."""
    if not renames:
        return
    if NO_DB:
        _p("  ⏭️  DB update skipped (--no-db)")
        return
    if not DB_URL:
        _p("  ⚠️  DATABASE_URL not set — skipping DB update")
        _p("       Set --db-url or DATABASE_URL env var to also fix the storage_objects table.")
        return

    try:
        from sqlalchemy import create_engine, text  # noqa: PLC0415
        engine = create_engine(DB_URL, pool_pre_ping=True)
        with engine.begin() as conn:
            for old_key, new_key in renames:
                result = conn.execute(
                    text("""
                        UPDATE storage_objects
                        SET    object_key = :new_key,
                               updated_at = now()
                        WHERE  object_key = :old_key
                          AND  object_type = 'ts_state'
                    """),
                    {"old_key": old_key, "new_key": new_key},
                )
                if result.rowcount:
                    _p(f"    🗄️  DB: updated {result.rowcount} row(s): {old_key} → {new_key}")
                else:
                    _p(f"    🗄️  DB: no row found for {old_key} (may have been stored under new key already)")
        _p("  ✅ DB update complete")
    except ImportError:
        _p("  ⚠️  sqlalchemy not available — skipping DB update")
    except Exception as exc:
        _p(f"  ❌ DB update failed: {exc}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    _p(f"\n{'[DRY RUN] ' if DRY else ''}XIOSYNC TS State Rename")
    _p(f"Drive root: {DRIVE_ROOT}")
    _p(f"TS states dir: {TS_DIR}")
    _p("─" * 60)

    if not DRIVE_ROOT.exists():
        _p(f"❌ Drive root not found: {DRIVE_ROOT}")
        _p("   Mount Drive first: from google.colab import drive; drive.mount('/content/drive')")
        sys.exit(1)

    _p("\nScanning for TS state files to rename…")
    candidates = _gather()

    if not candidates:
        _p("\n✅ Nothing to rename — all TS state files are already in canonical format.")
        return

    _p(f"\nFound {len(candidates)} file(s) to rename:\n")

    renamed: list[tuple[str, str]] = []  # (old_object_key, new_object_key)

    for src_path, canonical_key in candidates:
        old_rel   = str(src_path.relative_to(DRIVE_ROOT))
        new_abs   = DRIVE_ROOT / canonical_key
        conflict  = new_abs.exists() and new_abs != src_path

        _p(f"  {old_rel}")
        _p(f"    → {canonical_key}")

        if conflict:
            _p(f"    ⚠️  SKIP — target already exists: {new_abs}")
            continue

        if DRY:
            _p("    [dry-run — not executing]")
            renamed.append((old_rel, canonical_key))
            continue

        try:
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            src_path.rename(new_abs)
            _p("    ✅ Renamed")
            renamed.append((old_rel, canonical_key))
        except OSError as exc:
            _p(f"    ❌ Failed: {exc}")

    _p(f"\n{'Would rename' if DRY else 'Renamed'} {len(renamed)} file(s).\n")

    if renamed and not DRY:
        _p("Updating DB storage_objects records…")
        _db_update(renamed)

    _p("\n" + "─" * 60)
    _p("Done. Next Colab boot will restore the existing Tailscale node identity.")
    _p("Node will reconnect with its previous IP — no new machine will be created.")


if __name__ == "__main__":
    main()
