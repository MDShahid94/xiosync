"""Migration 0034 — Storage Providers + Object Index.

Universal org-configurable blob storage abstraction.

Design:
  - storage_providers: orgs register their preferred backends (Drive, R2, S3, local…)
  - storage_objects:   XIOSYNC tracks *what* is stored *where* (key, type, size, checksum)
                       The actual blob lives in the provider — XIOSYNC is the index.

Sharing model (consistent with vault + workflow_templates):
  storage_providers.organization_id = NULL  → platform-level shared provider
                                            (e.g. shared infra Drive folder, platform R2 bucket)
  storage_providers.organization_id = <id>  → org-private provider

Workers (Colab, VM, etc.) upload/download blobs directly to the configured provider,
then call POST /storage/objects to register the object in XIOSYNC's index.
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── storage_providers ─────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE storage_providers (
            id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id   UUID         REFERENCES organizations(id) ON DELETE CASCADE,
            -- NULL = platform-level shared provider (available to all orgs)

            name              TEXT         NOT NULL,
            -- Human label e.g. 'primary', 'cold-backup', 'colab-drive'

            provider_type     TEXT         NOT NULL,
            -- 'google_drive' | 'cloudflare_r2' | 's3' | 'supabase_storage' | 'local'

            config            JSONB        NOT NULL DEFAULT '{}',
            -- Provider-specific non-secret config:
            --   google_drive: { "folder_id": "...", "shared_drive_id": "..." }
            --   cloudflare_r2: { "account_id": "...", "bucket": "..." }
            --   s3:            { "endpoint": "...", "bucket": "...", "region": "..." }
            --   local:         { "base_path": "/data/blobs" }
            -- Credentials are referenced via vault_key (never stored inline)

            vault_key         TEXT,
            -- Key in vaulted_secrets holding the provider credential
            -- e.g. 'storage/primary/token'  →  service-account JSON or API token

            is_default        BOOL         NOT NULL DEFAULT false,
            is_writable       BOOL         NOT NULL DEFAULT true,
            -- false = read-only provider (cold archive, etc.)

            priority          INT          NOT NULL DEFAULT 100,
            -- Lower = preferred. Used when multiple providers match a query.

            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

            CONSTRAINT ck_storage_provider_type CHECK (
                provider_type IN ('google_drive','cloudflare_r2','s3','supabase_storage','local')
            ),
            UNIQUE (organization_id, name)
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX uq_storage_provider_platform_name
        ON storage_providers (name)
        WHERE organization_id IS NULL
    """)

    op.execute("""
        CREATE INDEX ix_storage_providers_org
        ON storage_providers (organization_id)
        WHERE organization_id IS NOT NULL
    """)

    # ── storage_objects ───────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE storage_objects (
            id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id   UUID         REFERENCES organizations(id) ON DELETE CASCADE,
            provider_id       UUID         NOT NULL REFERENCES storage_providers(id) ON DELETE RESTRICT,

            object_key        TEXT         NOT NULL,
            -- Logical key within the provider. For Drive: file path relative to folder_id.
            -- For R2/S3: object key. For local: relative path from base_path.

            object_type       TEXT         NOT NULL DEFAULT 'generic',
            -- 'chrome_profile' | 'ts_state' | 'session_export' | 'workflow_artifact' | 'generic'

            size_bytes        BIGINT,
            checksum_sha256   TEXT,
            content_type      TEXT,

            metadata          JSONB        NOT NULL DEFAULT '{}',
            -- Arbitrary searchable metadata:
            -- { "account_id": "...", "node_name": "colab-master", "profile_version": 3 }

            last_accessed_at  TIMESTAMPTZ,
            expires_at        TIMESTAMPTZ,  -- NULL = keep forever
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

            CONSTRAINT ck_storage_object_type CHECK (
                object_type IN ('chrome_profile','ts_state','session_export','workflow_artifact','generic')
            ),
            UNIQUE (provider_id, object_key)
        )
    """)

    op.execute("""
        CREATE INDEX ix_storage_objects_org_type
        ON storage_objects (organization_id, object_type)
    """)

    op.execute("""
        CREATE INDEX ix_storage_objects_expires
        ON storage_objects (expires_at)
        WHERE expires_at IS NOT NULL
    """)

    # GIN index for metadata search
    op.execute("""
        CREATE INDEX ix_storage_objects_metadata
        ON storage_objects USING GIN (metadata)
    """)

    op.execute("COMMENT ON TABLE storage_providers IS "
               "'Universal org-configurable blob storage backends. "
               "organization_id=NULL = platform-global shared provider.'")
    op.execute("COMMENT ON TABLE storage_objects IS "
               "'Object index: XIOSYNC tracks what exists where. Actual blobs live in the provider.'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS storage_objects CASCADE")
    op.execute("DROP TABLE IF EXISTS storage_providers CASCADE")
