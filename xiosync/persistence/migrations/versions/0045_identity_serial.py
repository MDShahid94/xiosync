"""0045 — Add permanent serial to identities table.

Serial is the canonical, immutable short identifier for a Chrome profile / identity.
Used in Drive file keys: PRFL-{serial:03d}_{username}.tar.gz

Design choices:
  - BIGINT DEFAULT nextval(): PostgreSQL assigns from a dedicated sequence.
    Row deletion does NOT reassign serials (sequence only goes forward).
  - Existing rows: backfilled sequentially ordered by created_at ASC so that
    older identities get lower serials (preserves the XIOBR/PRFL-001 ordering).
  - The sequence starts at the next value after the maximum backfilled value so
    new inserts always get unique serials above the existing range.
  - Unique constraints on (serial) and (organization_id, serial) enable fast
    key lookups for profile store operations.

Revision ID: 0045
Revises: 0044
"""
from alembic import op
import sqlalchemy as sa

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Add the column as nullable first (can't backfill existing rows otherwise)
    conn.execute(sa.text(
        "ALTER TABLE identities ADD COLUMN IF NOT EXISTS serial BIGINT"
    ))

    # 2. Backfill existing rows — serials ordered by created_at ASC
    #    (oldest identity gets lowest serial, matching XIOBR PRFL-001 convention)
    conn.execute(sa.text("""
        WITH ordered AS (
            SELECT id,
                   ROW_NUMBER() OVER (ORDER BY created_at ASC, id ASC) AS rn
            FROM identities
            WHERE serial IS NULL
        )
        UPDATE identities i
        SET serial = o.rn
        FROM ordered o
        WHERE i.id = o.id
    """))

    # 3. Create a named sequence starting above the existing max
    result = conn.execute(
        sa.text("SELECT COALESCE(MAX(serial), 0) FROM identities")
    )
    max_serial = result.scalar()
    next_val = int(max_serial or 0) + 1

    conn.execute(sa.text(f"""
        CREATE SEQUENCE IF NOT EXISTS identities_serial_seq
        START WITH {next_val}
        INCREMENT BY 1
        NO MINVALUE NO MAXVALUE
        CACHE 1
    """))

    # 4. Set column default to the sequence and make it NOT NULL
    conn.execute(sa.text(
        "ALTER TABLE identities "
        "ALTER COLUMN serial SET DEFAULT nextval('identities_serial_seq')"
    ))
    conn.execute(sa.text(
        "ALTER TABLE identities ALTER COLUMN serial SET NOT NULL"
    ))

    # 5. Attach sequence to column so it is dropped together with the column
    conn.execute(sa.text(
        "ALTER SEQUENCE identities_serial_seq OWNED BY identities.serial"
    ))

    # 6. Unique indexes for fast profile key lookups
    conn.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_identities_serial "
        "ON identities (serial)"
    ))
    conn.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_identities_org_serial "
        "ON identities (organization_id, serial) "
        "WHERE organization_id IS NOT NULL"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS uq_identities_org_serial"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uq_identities_serial"))
    conn.execute(sa.text("ALTER TABLE identities DROP COLUMN IF EXISTS serial"))
    conn.execute(sa.text("DROP SEQUENCE IF EXISTS identities_serial_seq"))
