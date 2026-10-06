"""0049 — Profile identity fields & domain-scoped session tracking.

Merges heads: 0048, 0024, c3d4e5f6a7b8

Revision ID: 0049
Revises: 0048, 0024, c3d4e5f6a7b8
Create Date: 2026-09-21

Adds materialization_mode and profile_version to identities.
Creates profile_domain_sets table for domain-scoped cookie health tracking.
"""
from __future__ import annotations

from alembic import op

revision = "0049"
down_revision = ("0048", "0024", "c3d4e5f6a7b8")
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""
    -- Add materialization mode and version tracking to identities
    ALTER TABLE identities ADD COLUMN IF NOT EXISTS materialization_mode TEXT NOT NULL DEFAULT 'tar_profile';
    ALTER TABLE identities ADD COLUMN IF NOT EXISTS profile_version INTEGER NOT NULL DEFAULT 0;

    -- Domain-scoped session state tracking
    CREATE TABLE profile_domain_sets (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        organization_id UUID NOT NULL REFERENCES organizations(id),
        identity_id UUID NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
        domain_pattern TEXT NOT NULL,
        is_valid BOOLEAN NOT NULL DEFAULT false,
        health_urgency TEXT NOT NULL DEFAULT 'none',
        cookie_count INTEGER NOT NULL DEFAULT 0,
        auth_method TEXT,
        parent_domain TEXT,
        last_verified_at TIMESTAMPTZ,
        last_refreshed_at TIMESTAMPTZ,
        vault_key TEXT,
        storage_object_key TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (organization_id, identity_id, domain_pattern)
    );

    ALTER TABLE profile_domain_sets ENABLE ROW LEVEL SECURITY;
    CREATE POLICY rls_profile_domain_sets ON profile_domain_sets
        USING (organization_id = current_setting('app.current_org_id')::UUID);
    """)

def downgrade():
    op.execute("""
    DROP TABLE IF EXISTS profile_domain_sets;
    ALTER TABLE identities DROP COLUMN IF EXISTS profile_version;
    ALTER TABLE identities DROP COLUMN IF EXISTS materialization_mode;
    """)
