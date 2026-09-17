"""Migration 0036 — Integration Providers.

Universal external DB/data-source connector registry.
Orgs register connections to external systems (Cloudflare D1, Supabase,
external Postgres, MySQL, etc.) for migration, federation, or live sync.

This is the foundation for the one-time D1 → XIOSYNC migration (Phase 9).
"""
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE integration_providers (
            id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id   UUID         REFERENCES organizations(id) ON DELETE CASCADE,

            name              TEXT         NOT NULL,
            provider_type     TEXT         NOT NULL,
            -- 'cloudflare_d1' | 'supabase' | 'postgres' | 'mysql' | 'sqlite' | 'mongodb'

            connection_config JSONB        NOT NULL DEFAULT '{}',
            -- Non-secret: host, port, database name, account_id, database_id
            -- Credentials referenced via vault_key

            vault_key         TEXT,
            -- Key in vaulted_secrets for the connection secret (API token, password, etc.)

            last_synced_at    TIMESTAMPTZ,
            last_sync_status  TEXT,        -- 'success' | 'partial' | 'failed'
            sync_stats        JSONB        NOT NULL DEFAULT '{}',
            -- { "rows_imported": 42, "tables": ["accounts","sessions"], "errors": 0 }

            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

            UNIQUE (organization_id, name),
            CONSTRAINT ck_integration_type CHECK (
                provider_type IN
                ('cloudflare_d1','supabase','postgres','mysql','sqlite','mongodb')
            )
        )
    """)

    op.execute("""
        CREATE INDEX ix_integration_providers_org
        ON integration_providers (organization_id)
    """)

    op.execute("COMMENT ON TABLE integration_providers IS "
               "'External DB/data-source connectors for migration and federation. "
               "Credentials via vault_key. organization_id=NULL = platform-level shared connector.'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS integration_providers CASCADE")
