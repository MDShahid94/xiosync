"""Migration 0037 — Worker config column.

Adds a JSONB config column to worker_enrollments so workers can pull
their full runtime configuration from XIOSYNC rather than a local file.

Sensitive values (tokens, passwords) are NOT stored here — they reference
vault_key paths in vaulted_secrets. The config is the non-sensitive runtime
parameters: ports, paths, feature flags, provider names, etc.
"""
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE worker_enrollments
        ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'
    """)

    op.execute("""
        COMMENT ON COLUMN worker_enrollments.config IS
        'Non-sensitive runtime config delivered to worker on GET /workers/{id}/config.
         Sensitive values use vault_key references, not inline values.'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE worker_enrollments DROP COLUMN IF EXISTS config")
