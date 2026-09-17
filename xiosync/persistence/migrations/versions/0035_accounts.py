"""Migration 0035 — Accounts + Session Credentials.

Universal identity + credential registry. NOT Google-specific, NOT V0-specific.
Covers any service account: google, v0, tailscale, github, cloudflare, twitter…

Design:
  accounts             — one row per (email, service) pair
  session_credentials  — live credential per account (cookie state, OAuth token, API key, etc.)
                         sensitive values are stored as vault references, never inline

Sharing model:
  accounts.organization_id = NULL  → platform-level shared account (available to all orgs)
  accounts.organization_id = <id>  → org-private account

  This allows an org to share a GitHub PAT or a Cloudflare account across
  all their member orgs without duplicating credentials.
"""
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── accounts ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE accounts (
            id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id  UUID         REFERENCES organizations(id) ON DELETE CASCADE,
            -- NULL = platform-level shared account

            email            TEXT         NOT NULL,
            service          TEXT         NOT NULL,
            -- 'google' | 'v0' | 'tailscale' | 'github' | 'cloudflare' |
            -- 'twitter' | 'linkedin' | 'openai' | 'anthropic' | <any string>

            display_name     TEXT,
            -- Human label (e.g. 'Main Google Workspace', 'Org GitHub Bot')

            tier             TEXT,
            -- Service-specific plan: 'Pro' | 'Starter' for v0, 'Workspace' for Google, etc.

            state            TEXT         NOT NULL DEFAULT 'active',
            -- 'active' | 'suspended' | 'banned' | 'unverified' | 'expired'

            metadata         JSONB        NOT NULL DEFAULT '{}',
            -- Service-specific non-sensitive fields:
            -- google: { "recovery_email": "...", "2fa_enabled": true }
            -- tailscale: { "tailnet": "xiogrid.dev", "node_type": "exit" }

            tags             TEXT[]       NOT NULL DEFAULT '{}',
            -- Free-form tags for grouping/search: ['colab', 'master', 'v0-pro']

            last_used_at     TIMESTAMPTZ,
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),

            UNIQUE (organization_id, email, service),

            CONSTRAINT ck_account_state CHECK (
                state IN ('active','suspended','banned','unverified','expired')
            )
        )
    """)

    # Separate unique index for platform-level accounts (NULL org)
    op.execute("""
        CREATE UNIQUE INDEX uq_accounts_platform
        ON accounts (email, service)
        WHERE organization_id IS NULL
    """)

    op.execute("""
        CREATE INDEX ix_accounts_org_service
        ON accounts (organization_id, service)
        WHERE organization_id IS NOT NULL
    """)

    op.execute("CREATE INDEX ix_accounts_state ON accounts (state)")
    op.execute("CREATE INDEX ix_accounts_tags ON accounts USING GIN (tags)")

    # ── session_credentials ───────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE session_credentials (
            id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id     UUID         REFERENCES organizations(id) ON DELETE CASCADE,

            account_id          UUID         NOT NULL
                                             REFERENCES accounts(id) ON DELETE CASCADE,

            credential_type     TEXT         NOT NULL,
            -- 'cookie_state'   — Playwright storageState JSON (cookies + localStorage)
            -- 'oauth_token'    — OAuth2 access + refresh token pair
            -- 'api_key'        — Static API key / bearer token
            -- 'ssh_key'        — SSH public/private key pair
            -- 'totp_secret'    — TOTP seed (32-char Base32)
            -- 'password'       — Account password (use sparingly)

            vault_key           TEXT,
            -- Key in vaulted_secrets holding the actual credential payload.
            -- e.g. 'sessions/google/user@gmail.com/cookie_state'
            -- Credentials NEVER stored inline here — always via vault.

            storage_object_key  TEXT,
            -- For large blobs (Chrome profiles, SSH key files stored in Drive/R2):
            -- references storage_objects.object_key in the org's primary storage provider.

            health_score        FLOAT        NOT NULL DEFAULT 1.0,
            -- 0.0 = dead / needs refresh, 1.0 = fully healthy
            -- Updated by workers after health checks.

            last_refreshed_at   TIMESTAMPTZ,
            -- When the credential was last verified / refreshed
            expires_at          TIMESTAMPTZ,
            -- NULL = does not expire (API keys, TOTP secrets)

            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),

            CONSTRAINT ck_session_cred_type CHECK (
                credential_type IN
                ('cookie_state','oauth_token','api_key','ssh_key','totp_secret','password')
            ),
            -- One credential record per (account, type) — rotate via UPDATE
            UNIQUE (account_id, credential_type)
        )
    """)

    op.execute("""
        CREATE INDEX ix_session_creds_account
        ON session_credentials (account_id)
    """)
    op.execute("""
        CREATE INDEX ix_session_creds_health
        ON session_credentials (health_score)
        WHERE health_score < 0.8
    """)
    op.execute("""
        CREATE INDEX ix_session_creds_expires
        ON session_credentials (expires_at)
        WHERE expires_at IS NOT NULL
    """)

    op.execute("COMMENT ON TABLE accounts IS "
               "'Universal service account registry. organization_id=NULL = platform-global shared account.'")
    op.execute("COMMENT ON TABLE session_credentials IS "
               "'Live credentials per account. Sensitive values via vault_key reference — never stored inline.'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_credentials CASCADE")
    op.execute("DROP TABLE IF EXISTS accounts CASCADE")
