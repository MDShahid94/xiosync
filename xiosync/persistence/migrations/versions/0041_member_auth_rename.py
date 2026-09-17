"""0041 — Rename auth_identities → member_auth.

Resolves the naming collision between:
  auth_identities — XIOSYNC's own email+password login table for human members
  identities      — universal external-platform identity registry

New name: member_auth
  - "member" scopes it to XIOSYNC org members
  - "auth"   clearly marks it as authentication/login credentials
  - No conflict with `credentials` (external secrets) or `identities` (external platforms)

Cascade: Postgres RENAME TABLE automatically preserves FKs from sessions and memberships.
Constraint names retain their old names (cosmetic, not functional).
"""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Rename the table — Postgres automatically updates FK references by OID
    conn.execute(sa.text("ALTER TABLE auth_identities RENAME TO member_auth"))

    # Rename the associated sequences/indexes for clarity
    conn.execute(sa.text(
        "ALTER INDEX IF EXISTS auth_identities_pkey RENAME TO member_auth_pkey"
    ))

    # Rename the FK constraint on sessions for clarity
    conn.execute(sa.text("""
        ALTER TABLE sessions
        RENAME CONSTRAINT fk_sessions_auth_identity_same_org
        TO fk_sessions_member_auth_same_org
    """))

    # Rename partial index on sessions if it exists
    conn.execute(sa.text("""
        ALTER INDEX IF EXISTS ix_sessions_auth_identity_active
        RENAME TO ix_sessions_member_auth_active
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        ALTER TABLE sessions
        RENAME CONSTRAINT fk_sessions_member_auth_same_org
        TO fk_sessions_auth_identity_same_org
    """))
    conn.execute(sa.text("""
        ALTER INDEX IF EXISTS ix_sessions_member_auth_active
        RENAME TO ix_sessions_auth_identity_active
    """))
    conn.execute(sa.text("ALTER INDEX IF EXISTS member_auth_pkey RENAME TO auth_identities_pkey"))
    conn.execute(sa.text("ALTER TABLE member_auth RENAME TO auth_identities"))
