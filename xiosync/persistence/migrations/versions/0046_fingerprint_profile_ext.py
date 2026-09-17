"""0046 — Extend xiogrid_fingerprint_profiles for deeper anti-detection.

Adds three columns used by fingerprint.py build_init_script() new JS layers:

  timezone        TEXT NOT NULL DEFAULT 'America/New_York'
      IANA tz string. Injected into Intl.DateTimeFormat().resolvedOptions().timeZone
      and Date().getTimezoneOffset(). Must match the PPPoE exit-node's geo-IP region
      so timezone + IP are geographically consistent.

  battery_level   FLOAT NOT NULL DEFAULT 0.72
      Injected into navigator.getBattery() → BatteryManager.level.
      0.72 = plausible mid-charge, not suspiciously full (1.0) or empty (0.0).

  connection_type TEXT NOT NULL DEFAULT 'wifi'
      Injected into navigator.connection.effectiveType.
      Valid values: 'wifi' | '4g' | 'ethernet'.

Also backfills all existing rows with macOS-appropriate defaults,
then updates the seed profiles to use US East Coast / West Coast timezones.

Revision ID: 0046
Revises: 0045
"""
from alembic import op
import sqlalchemy as sa

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Add columns with server defaults (safe for existing rows)
    conn.execute(sa.text("""
        ALTER TABLE xiogrid_fingerprint_profiles
        ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'America/New_York'
    """))
    conn.execute(sa.text("""
        ALTER TABLE xiogrid_fingerprint_profiles
        ADD COLUMN IF NOT EXISTS battery_level FLOAT NOT NULL DEFAULT 0.72
    """))
    conn.execute(sa.text("""
        ALTER TABLE xiogrid_fingerprint_profiles
        ADD COLUMN IF NOT EXISTS connection_type TEXT NOT NULL DEFAULT 'wifi'
    """))

    # 2. Set macOS profiles to realistic US timezone spread
    #    macOS profiles rotate through East/Central/Mountain/West so that
    #    different exit-node IPs get geographically matching timezones.
    conn.execute(sa.text("""
        UPDATE xiogrid_fingerprint_profiles
        SET timezone = CASE
            WHEN name ILIKE '%mac%1%' OR name ILIKE '%mac%east%' THEN 'America/New_York'
            WHEN name ILIKE '%mac%2%' OR name ILIKE '%mac%central%' THEN 'America/Chicago'
            WHEN name ILIKE '%mac%3%' OR name ILIKE '%mac%mountain%' THEN 'America/Denver'
            WHEN name ILIKE '%mac%4%' OR name ILIKE '%mac%west%' THEN 'America/Los_Angeles'
            WHEN os = 'macos' THEN 'America/New_York'
            WHEN os = 'windows' THEN 'America/Chicago'
            WHEN os = 'linux' THEN 'America/Los_Angeles'
            WHEN os = 'android' THEN 'America/New_York'
            ELSE 'America/New_York'
        END
    """))

    # 3. Battery level: macOS typically shows ~80%, mobile ~60%, desktop ~72%
    conn.execute(sa.text("""
        UPDATE xiogrid_fingerprint_profiles
        SET battery_level = CASE
            WHEN is_mobile THEN 0.61
            WHEN os = 'macos' THEN 0.83
            ELSE 0.72
        END
    """))

    # 4. Connection type: macOS/Windows = wifi, Linux desktop = ethernet, mobile = 4g
    conn.execute(sa.text("""
        UPDATE xiogrid_fingerprint_profiles
        SET connection_type = CASE
            WHEN is_mobile THEN '4g'
            WHEN os = 'linux' THEN 'ethernet'
            ELSE 'wifi'
        END
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "ALTER TABLE xiogrid_fingerprint_profiles DROP COLUMN IF EXISTS timezone"
    ))
    conn.execute(sa.text(
        "ALTER TABLE xiogrid_fingerprint_profiles DROP COLUMN IF EXISTS battery_level"
    ))
    conn.execute(sa.text(
        "ALTER TABLE xiogrid_fingerprint_profiles DROP COLUMN IF EXISTS connection_type"
    ))
