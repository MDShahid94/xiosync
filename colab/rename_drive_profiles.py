#!/usr/bin/env python3
"""rename_drive_profiles.py — One-time Drive profile rename + DB sync.

Renames Chrome profile tarballs in Google Drive from legacy naming conventions
to the canonical XIOSYNC format: PRFL-{serial:03d}_{username}.tar.gz

Handles three source formats:
  1. XIOBR R2 legacy  : username.tar.gz  (e.g. karmareturnsfromallsides.tar.gz)
  2. XIOSYNC hex-slug : PRFL_{hex16}.tar.gz  (e.g. PRFL_a1b2c3d4e5f6a7b8.tar.gz)
  3. Already canonical: PRFL-NNN_username.tar.gz  (skipped)

After rename, updates credentials.storage_object_key in the DB so
profile_store.py's lookup_drive_object_key() always hits the fast path.

Usage:
    python3 rename_drive_profiles.py [--dry-run] [--db-url <postgres_dsn>]

Run this ONCE on the Colab master node with Drive FUSE mounted.
Safe to re-run: canonical files are skipped, DB is idempotent (ON CONFLICT DO UPDATE).
"""
from __future__ import annotations
import argparse, os, re, sys, uuid
from pathlib import Path

# ── CLI ────────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true", help="Print actions without executing")
ap.add_argument("--drive-root", default=os.environ.get("XIO_DRIVE_ROOT",
                "/content/drive/MyDrive/XIOSYNC-Shared"))
ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL", ""),
                help="PostgreSQL DSN — needed to update storage_object_key in DB")
args = ap.parse_args()

DRY = args.dry_run
DRIVE_ROOT = args.drive_root
PROFILES_DIR = Path(DRIVE_ROOT) / "chrome_profiles"

def _p(*a): print(*a, flush=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_username(identifier: str) -> str:
    """Match profile_store._normalize_username() exactly."""
    u = identifier.split("@")[0]
    u = re.sub(r"^PRFL-\d+_", "", u, flags=re.IGNORECASE)
    u = re.sub(r"[^a-zA-Z0-9]", "_", u)
    u = re.sub(r"_+", "_", u).strip("_")
    return u.lower()


def _canonical_name(serial: int, identifier: str) -> str:
    return f"PRFL-{serial:03d}_{_normalize_username(identifier)}.tar.gz"


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _load_identity_map(conn) -> dict[str, dict]:
    """Load all identities with their serial from the DB.
    Returns {identifier: {serial, id, identifier}} mapping.
    Also includes UUID-based hex lookup for PRFL_{hex16} slugs.
    """
    rows = conn.execute("""
        SELECT id::text, identifier, serial FROM identities WHERE serial IS NOT NULL
    """).fetchall()
    by_identifier: dict[str, dict] = {}
    by_id_slug: dict[str, dict] = {}
    for row in rows:
        r = {"id": row[0], "identifier": row[1], "serial": row[2]}
        by_identifier[_normalize_username(row[1])] = r
        # hex slug = first 16 chars of uuid without dashes
        slug = row[0].replace("-", "")[:16]
        by_id_slug[slug] = r
    return by_identifier, by_id_slug


def _update_db(conn, identity_id: str, new_key: str) -> None:
    conn.execute("""
        INSERT INTO credentials (id, organization_id, identity_id, credential_type,
                                 label, storage_object_key, created_at, updated_at)
        VALUES (gen_random_uuid(), NULL, %s::uuid, 'cookie_state', 'default', %s,
                now(), now())
        ON CONFLICT (identity_id, credential_type, label)
        DO UPDATE SET storage_object_key = EXCLUDED.storage_object_key,
                      updated_at = now()
    """, (identity_id, new_key))


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    if not PROFILES_DIR.exists():
        _p(f"❌ Profiles directory not found: {PROFILES_DIR}")
        sys.exit(1)

    _p("=" * 60)
    _p("  XIOSYNC Profile Drive Rename")
    _p("=" * 60)
    _p(f"  Drive profiles : {PROFILES_DIR}")
    _p(f"  Mode           : {'DRY RUN' if DRY else 'LIVE'}")
    _p("")

    # Optional DB connection
    conn = None
    if args.db_url:
        try:
            import psycopg2
            conn = psycopg2.connect(args.db_url)
            conn.autocommit = False
            _p(f"  DB             : connected")
        except Exception as e:
            _p(f"  ⚠️  DB connect failed: {e} — will rename files only (no DB update)")
    else:
        _p("  DB             : not connected (pass --db-url to update storage_object_key)")

    by_identifier, by_id_slug = {}, {}
    if conn:
        by_identifier, by_id_slug = _load_identity_map(conn.cursor())

    # Regex patterns
    RE_CANONICAL = re.compile(r"^PRFL-(\d{3})_(.+)\.tar\.gz$")
    RE_HEX_SLUG  = re.compile(r"^PRFL_([0-9a-f]{16})\.tar\.gz$", re.IGNORECASE)
    RE_LEGACY    = re.compile(r"^(.+)\.tar\.gz$")

    files = sorted(PROFILES_DIR.glob("*.tar.gz"))
    _p(f"  Found {len(files)} .tar.gz files in chrome_profiles/")
    _p("")

    renamed = skipped = unknown = 0

    for f in files:
        name = f.name

        # Already canonical — skip
        if RE_CANONICAL.match(name):
            _p(f"  ✅ Already canonical: {name}")
            skipped += 1
            continue

        identity = None
        new_name = None

        # XIOSYNC hex-slug: PRFL_{hex16}.tar.gz
        m = RE_HEX_SLUG.match(name)
        if m:
            slug = m.group(1).lower()
            identity = by_id_slug.get(slug)
            if identity:
                new_name = _canonical_name(identity["serial"], identity["identifier"])
            else:
                _p(f"  ⚠️  Hex-slug {name}: no matching identity in DB — skipping")
                unknown += 1
                continue

        # Legacy: username.tar.gz or email.tar.gz
        if not new_name:
            m = RE_LEGACY.match(name)
            if m:
                raw = m.group(1)
                norm = _normalize_username(raw)
                identity = by_identifier.get(norm)
                if identity:
                    new_name = _canonical_name(identity["serial"], identity["identifier"])
                else:
                    _p(f"  ⚠️  Legacy {name}: no matching identity (normalized='{norm}') — skipping")
                    unknown += 1
                    continue

        if not new_name:
            _p(f"  ❓ Unknown format: {name} — skipping")
            unknown += 1
            continue

        new_path = PROFILES_DIR / new_name
        new_key  = f"chrome_profiles/{new_name}"

        if new_path.exists() and new_path != f:
            _p(f"  ⚠️  Target already exists: {new_name} — skipping {name}")
            unknown += 1
            continue

        action = "RENAME" if not DRY else "DRY-RENAME"
        _p(f"  {action}: {name} → {new_name}")
        if not DRY:
            f.rename(new_path)
            if conn and identity:
                cur = conn.cursor()
                _update_db(cur, identity["id"], new_key)

        renamed += 1

    if conn and not DRY:
        conn.commit()
        conn.close()

    _p("")
    _p("=" * 60)
    _p(f"  Done: {renamed} renamed, {skipped} already canonical, {unknown} skipped/unknown")
    if DRY:
        _p("  (dry run — no files or DB rows were modified)")
    _p("=" * 60)


if __name__ == "__main__":
    main()
