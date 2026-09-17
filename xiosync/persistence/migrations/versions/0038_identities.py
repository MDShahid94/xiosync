"""0038 — Universalize identity + credential registry.

Removes XIOBR-specific contamination from the platform schema:

  accounts          → identities       (universal external-identity registry)
    email           → identifier       (any identifier: email, username, handle, ...)
    service         → platform         (open text: google, github, stripe, custom, ...)
    tier            → dropped          (org-policy concern; data moved to metadata)
    ck_account_state CHECK → dropped   (open state, validated in service layer)

  session_credentials → credentials   (universal credential registry)
    account_id      → identity_id     (FK renamed to match new table)
    ck_session_cred_type CHECK → dropped (credential_type is free-form)

  integration_providers:
    ck_integration_type CHECK → dropped (open provider type)

  storage_providers:
    ck_storage_provider_type CHECK → dropped (open provider type)

  vault key namespace:
    accounts/{id}/... → identities/{id}/... (vaulted_secrets.key + credentials.vault_key)
"""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Drop closed-set CHECK constraints (raw SQL avoids Alembic name-prefixing) ──
    conn.execute(sa.text("ALTER TABLE accounts DROP CONSTRAINT IF EXISTS ck_account_state"))
    conn.execute(sa.text("ALTER TABLE session_credentials DROP CONSTRAINT IF EXISTS ck_session_cred_type"))
    conn.execute(sa.text("ALTER TABLE integration_providers DROP CONSTRAINT IF EXISTS ck_integration_type"))
    conn.execute(sa.text("ALTER TABLE storage_providers DROP CONSTRAINT IF EXISTS ck_storage_provider_type"))

    # ── 2. accounts: move tier → metadata, drop tier column ─────────────────────
    conn.execute(sa.text("""
        UPDATE accounts
        SET metadata = metadata || jsonb_build_object('xiobr_tier', tier)
        WHERE tier IS NOT NULL
    """))
    conn.execute(sa.text("ALTER TABLE accounts DROP COLUMN IF EXISTS tier"))

    # ── 3. accounts: rename columns ──────────────────────────────────────────────
    conn.execute(sa.text("ALTER TABLE accounts RENAME COLUMN email TO identifier"))
    conn.execute(sa.text("ALTER TABLE accounts RENAME COLUMN service TO platform"))

    # ── 4. Drop all accounts indexes (will recreate with correct names) ──────────
    conn.execute(sa.text("ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_organization_id_email_service_key"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uq_accounts_platform"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_accounts_org_service"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_accounts_state"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_accounts_tags"))

    # ── 5. Rename accounts → identities ──────────────────────────────────────────
    conn.execute(sa.text("ALTER TABLE accounts RENAME TO identities"))

    # Recreate indexes/unique constraints with correct names
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX identities_organization_id_identifier_platform_key
        ON identities (organization_id, identifier, platform)
    """))
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX uq_identities_platform
        ON identities (identifier, platform)
        WHERE organization_id IS NULL
    """))
    conn.execute(sa.text("""
        CREATE INDEX ix_identities_org_platform
        ON identities (organization_id, platform)
        WHERE organization_id IS NOT NULL
    """))
    conn.execute(sa.text("CREATE INDEX ix_identities_state ON identities (state)"))
    conn.execute(sa.text("CREATE INDEX ix_identities_tags ON identities USING GIN (tags)"))

    # ── 6. session_credentials: rename account_id → identity_id ─────────────────
    # Drop FK + unique + indexes first
    conn.execute(sa.text("""
        ALTER TABLE session_credentials
        DROP CONSTRAINT IF EXISTS session_credentials_account_id_fkey
    """))
    conn.execute(sa.text("""
        ALTER TABLE session_credentials
        DROP CONSTRAINT IF EXISTS session_credentials_account_id_credential_type_key
    """))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_session_creds_account"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_session_creds_expires"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_session_creds_health"))

    conn.execute(sa.text("""
        ALTER TABLE session_credentials RENAME COLUMN account_id TO identity_id
    """))

    # Rename table
    conn.execute(sa.text("ALTER TABLE session_credentials RENAME TO credentials"))

    # Recreate FK + unique + indexes with new names
    conn.execute(sa.text("""
        ALTER TABLE credentials
        ADD CONSTRAINT credentials_identity_id_fkey
        FOREIGN KEY (identity_id) REFERENCES identities(id) ON DELETE CASCADE
    """))
    conn.execute(sa.text("""
        ALTER TABLE credentials
        ADD CONSTRAINT credentials_identity_id_credential_type_key
        UNIQUE (identity_id, credential_type)
    """))
    conn.execute(sa.text("CREATE INDEX ix_credentials_identity ON credentials (identity_id)"))
    conn.execute(sa.text("""
        CREATE INDEX ix_credentials_expires ON credentials (expires_at)
        WHERE expires_at IS NOT NULL
    """))
    conn.execute(sa.text("""
        CREATE INDEX ix_credentials_health ON credentials (health_score)
        WHERE health_score < 0.8
    """))

    # ── 7. Rewrite vault key namespace accounts/ → identities/ ───────────────────
    conn.execute(sa.text("""
        UPDATE vaulted_secrets
        SET key = 'identities/' || substring(key FROM 10)
        WHERE key LIKE 'accounts/%'
    """))
    conn.execute(sa.text("""
        UPDATE credentials
        SET vault_key = 'identities/' || substring(vault_key FROM 10)
        WHERE vault_key LIKE 'accounts/%'
    """))


def downgrade() -> None:
    conn = op.get_bind()

    # Reverse vault key namespace
    conn.execute(sa.text("""
        UPDATE credentials
        SET vault_key = 'accounts/' || substring(vault_key FROM 12)
        WHERE vault_key LIKE 'identities/%'
    """))
    conn.execute(sa.text("""
        UPDATE vaulted_secrets
        SET key = 'accounts/' || substring(key FROM 12)
        WHERE key LIKE 'identities/%'
    """))

    # Rename credentials → session_credentials
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_credentials_identity"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_credentials_expires"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_credentials_health"))
    conn.execute(sa.text("ALTER TABLE credentials DROP CONSTRAINT IF EXISTS credentials_identity_id_fkey"))
    conn.execute(sa.text("ALTER TABLE credentials DROP CONSTRAINT IF EXISTS credentials_identity_id_credential_type_key"))
    conn.execute(sa.text("ALTER TABLE credentials RENAME COLUMN identity_id TO account_id"))
    conn.execute(sa.text("ALTER TABLE credentials RENAME TO session_credentials"))
    conn.execute(sa.text("""
        ALTER TABLE session_credentials
        ADD CONSTRAINT session_credentials_account_id_fkey
        FOREIGN KEY (account_id) REFERENCES identities(id) ON DELETE CASCADE
    """))
    conn.execute(sa.text("""
        ALTER TABLE session_credentials
        ADD CONSTRAINT session_credentials_account_id_credential_type_key
        UNIQUE (account_id, credential_type)
    """))
    conn.execute(sa.text("CREATE INDEX ix_session_creds_account ON session_credentials (account_id)"))

    # Rename identities → accounts
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_identities_state"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_identities_tags"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_identities_org_platform"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uq_identities_platform"))
    conn.execute(sa.text("DROP INDEX IF EXISTS identities_organization_id_identifier_platform_key"))
    conn.execute(sa.text("ALTER TABLE identities RENAME COLUMN identifier TO email"))
    conn.execute(sa.text("ALTER TABLE identities RENAME COLUMN platform TO service"))
    conn.execute(sa.text("ALTER TABLE identities RENAME TO accounts"))
    conn.execute(sa.text("ALTER TABLE accounts ADD COLUMN tier TEXT"))
    conn.execute(sa.text("UPDATE accounts SET tier = metadata->>'xiobr_tier' WHERE metadata ? 'xiobr_tier'"))
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX accounts_organization_id_email_service_key
        ON accounts (organization_id, email, service)
    """))
    conn.execute(sa.text("CREATE INDEX ix_accounts_state ON accounts (state)"))
    conn.execute(sa.text("CREATE INDEX ix_accounts_tags ON accounts USING GIN (tags)"))
