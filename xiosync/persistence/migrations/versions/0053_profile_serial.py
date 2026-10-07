"""0053 — Profile serial column + storage key normalization.

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-21

Adds ``identities.profile_serial`` — a stable, email-independent integer
serial (``PRFL-{profile_serial:03d}``) that is the single source of truth
for profile naming, independent of the Google account identifier.

Also normalises ``credentials.storage_object_key`` values:
  - Old prefix  ``chrome_profiles/``  → ``xiosync_profiles/``
  - Strips email suffix from filename  ``PRFL-001_shahid_raiganj.tar.gz``
                                      → ``PRFL-001.tar.gz``

The backfill extracts the numeric serial from existing filenames so that
pre-existing profiles keep their PRFL-NNN number.  Any identity with no
``storage_object_key`` yet receives the next value from the sequence.
"""

from __future__ import annotations

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- ── 1. Create profile serial sequence ────────────────────────────────────
    CREATE SEQUENCE IF NOT EXISTS profile_serial_seq
        START 1 INCREMENT 1 MINVALUE 1 NO MAXVALUE CACHE 1;

    -- ── 2. Add profile_serial column (nullable first, fill, then constrain) ──
    ALTER TABLE identities
        ADD COLUMN IF NOT EXISTS profile_serial INTEGER;

    -- ── 3. Backfill from existing storage_object_key filenames ───────────────
    -- Extract the NNN from patterns like:
    --   chrome_profiles/PRFL-001_shahid_raiganj.tar.gz
    --   xiosync_profiles/PRFL-007_thewitnessone.tar.gz
    UPDATE identities i
    SET profile_serial = (
        regexp_match(c.storage_object_key, 'PRFL-0*([1-9][0-9]*)\\.tar\\.gz$')
    )[1]::integer
    FROM credentials c
    WHERE c.identity_id = i.id
      AND c.credential_type = 'cookie_state'
      AND c.storage_object_key ~ 'PRFL-[0-9]+'
      AND i.profile_serial IS NULL;

    -- ── 4. Assign sequence values to identities that have no profile yet ─────
    UPDATE identities
    SET profile_serial = nextval('profile_serial_seq')
    WHERE profile_serial IS NULL;

    -- Advance sequence past the max backfilled value so next assignment
    -- doesn't collide with any backfilled number.
    SELECT setval(
        'profile_serial_seq',
        GREATEST(COALESCE((SELECT MAX(profile_serial) FROM identities), 0), 1)
    );

    -- ── 5. Add unique / not-null constraints ─────────────────────────────────
    ALTER TABLE identities
        ALTER COLUMN profile_serial SET NOT NULL,
        ALTER COLUMN profile_serial SET DEFAULT nextval('profile_serial_seq'),
        ADD CONSTRAINT uq_identities_profile_serial UNIQUE (profile_serial);

    -- ── 6. Normalise storage_object_key prefix + strip email suffixes ─────────
    -- Step A: rename prefix chrome_profiles/ → xiosync_profiles/
    UPDATE credentials
    SET storage_object_key = regexp_replace(
            storage_object_key,
            '^chrome_profiles/',
            'xiosync_profiles/'
        ),
        updated_at = now()
    WHERE storage_object_key LIKE 'chrome_profiles/%';

    -- Step B: strip email/username suffix from filenames
    --   PRFL-001_1x2xx3xxx4xxxx5xxxxx6xxxxxx789.tar.gz → PRFL-001.tar.gz
    --   PRFL-003_shahid_raiganj.tar.gz                 → PRFL-003.tar.gz
    UPDATE credentials
    SET storage_object_key = regexp_replace(
            storage_object_key,
            '(xiosync_profiles/PRFL-[0-9]+)_[^/]+\\.tar\\.gz$',
            '\\1.tar.gz'
        ),
        updated_at = now()
    WHERE storage_object_key ~ 'xiosync_profiles/PRFL-[0-9]+_[^/]+\\.tar\\.gz$';
    """)


def downgrade() -> None:
    op.execute("""
    -- Reverse storage key changes is destructive (we can't recover the email suffix)
    -- so downgrade only removes the column / sequence.
    ALTER TABLE identities
        DROP CONSTRAINT IF EXISTS uq_identities_profile_serial,
        ALTER COLUMN profile_serial DROP NOT NULL,
        ALTER COLUMN profile_serial DROP DEFAULT,
        DROP COLUMN IF EXISTS profile_serial;

    DROP SEQUENCE IF EXISTS profile_serial_seq;
    """)
