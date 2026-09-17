"""Migration 0033 — Vault: vaulted_secrets table.

Universal per-org encrypted secret store. Replaces every pattern in the
codebase where secrets are hardcoded, stored in .env only, or kept in
external services (CF D1, Supabase). Any org, any secret type.

Sharing model (consistent with platform-global pattern):
  organization_id = NULL  → platform-level secret (shared infra credentials,
                             available to all orgs under RBAC control)
  organization_id = <uuid> → org-scoped, private to that org
"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE vaulted_secrets (
            id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id  UUID        REFERENCES organizations(id) ON DELETE CASCADE,
            -- NULL org_id = platform-level (shared across orgs, RBAC controlled)

            key              TEXT        NOT NULL,
            -- Namespaced key e.g. 'cf_api_token', 'totp/user@gmail.com', 'gh_pat'

            ciphertext       BYTEA       NOT NULL,   -- AES-256-GCM encrypted value
            iv               BYTEA       NOT NULL,   -- 12-byte nonce
            auth_tag         BYTEA       NOT NULL,   -- 16-byte GCM auth tag

            secret_type      TEXT        NOT NULL DEFAULT 'generic',
            -- CHECK below: 'generic' | 'credential' | 'token' | 'totp' | 'api_key' | 'ssh_key' | 'oauth'

            description      TEXT,                   -- human-readable purpose
            expires_at       TIMESTAMPTZ,            -- NULL = never expires
            rotated_at       TIMESTAMPTZ,            -- last rotation timestamp
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

            UNIQUE (organization_id, key),           -- one key per org (NULL treated as distinct scope)
            CONSTRAINT ck_vault_type CHECK (
                secret_type IN ('generic','credential','token','totp','api_key','ssh_key','oauth')
            )
        )
    """)

    # Separate unique index for platform-level secrets (org_id IS NULL)
    op.execute("""
        CREATE UNIQUE INDEX uq_vault_platform_key
        ON vaulted_secrets (key)
        WHERE organization_id IS NULL
    """)

    op.execute("""
        CREATE INDEX ix_vault_org_type
        ON vaulted_secrets (organization_id, secret_type)
        WHERE organization_id IS NOT NULL
    """)

    op.execute("""
        CREATE INDEX ix_vault_expires
        ON vaulted_secrets (expires_at)
        WHERE expires_at IS NOT NULL
    """)

    op.execute("COMMENT ON TABLE vaulted_secrets IS "
               "'Universal per-org encrypted secret store. AES-256-GCM. "
               "organization_id=NULL means platform-global shared secret.'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS vaulted_secrets CASCADE")
