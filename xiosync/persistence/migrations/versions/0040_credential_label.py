"""0040 — Add label to credentials + relax UNIQUE constraint.

Previously: UNIQUE (identity_id, credential_type)
Limitation: one credential per type per identity — blocks:
  - Multiple API keys (prod vs sandbox)
  - Multiple OAuth tokens (different scopes or apps)
  - Multiple SSH keys for different hosts
  - Rotation staging (hold old + new simultaneously)

After: UNIQUE (identity_id, credential_type, label)
  label defaults to 'default' — all existing rows unaffected.
  Callers that don't specify a label get the same single-credential
  behavior as before. Callers that need multiple can pass a label
  (e.g. 'sandbox', 'scope:calendar', 'host:vm-01').

Also adds identity_id FK to storage_objects so blobs can be linked
to the identity they belong to (Chrome profiles, session exports, etc.)
"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Add label column to credentials ───────────────────────────────────
    conn.execute(sa.text("""
        ALTER TABLE credentials ADD COLUMN IF NOT EXISTS
        label TEXT NOT NULL DEFAULT 'default'
    """))

    # ── 2. Drop the old (identity_id, credential_type) unique constraint ──────
    conn.execute(sa.text("""
        ALTER TABLE credentials
        DROP CONSTRAINT IF EXISTS credentials_identity_id_credential_type_key
    """))

    # ── 3. Add the new (identity_id, credential_type, label) unique constraint ─
    conn.execute(sa.text("""
        ALTER TABLE credentials
        ADD CONSTRAINT credentials_identity_id_type_label_key
        UNIQUE (identity_id, credential_type, label)
    """))

    # ── 4. Add identity_id FK to storage_objects (nullable — not all objects ──
    #       belong to an identity, e.g. org-level artifacts)
    conn.execute(sa.text("""
        ALTER TABLE storage_objects
        ADD COLUMN IF NOT EXISTS identity_id UUID
        REFERENCES identities(id) ON DELETE SET NULL
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_storage_objects_identity
        ON storage_objects (identity_id)
        WHERE identity_id IS NOT NULL
    """))


def downgrade() -> None:
    conn = op.get_bind()

    # Reverse storage_objects change
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_storage_objects_identity"))
    conn.execute(sa.text("ALTER TABLE storage_objects DROP COLUMN IF EXISTS identity_id"))

    # Reverse credentials change
    conn.execute(sa.text("""
        ALTER TABLE credentials
        DROP CONSTRAINT IF EXISTS credentials_identity_id_type_label_key
    """))
    conn.execute(sa.text("""
        ALTER TABLE credentials
        ADD CONSTRAINT credentials_identity_id_credential_type_key
        UNIQUE (identity_id, credential_type)
    """))
    conn.execute(sa.text("ALTER TABLE credentials DROP COLUMN IF EXISTS label"))
