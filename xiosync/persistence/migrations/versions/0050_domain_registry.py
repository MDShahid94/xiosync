"""0050 — Universal domain registry.

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-21

Org-scoped domain registry for managing main + dependent auth domains.
Seeded with platform-global defaults for Google, v0, Tailscale, GitHub, Vercel, Proton.
"""
from __future__ import annotations

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""
    CREATE TABLE domain_registrations (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        organization_id UUID REFERENCES organizations(id),
        domain_pattern TEXT NOT NULL,
        auth_method TEXT NOT NULL DEFAULT 'direct',
        parent_domain TEXT,
        cookie_domain_patterns JSONB NOT NULL DEFAULT '[]'::jsonb,
        display_name TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (organization_id, domain_pattern)
    );

    -- Seed with platform-global defaults (organization_id IS NULL)
    INSERT INTO domain_registrations (organization_id, domain_pattern, auth_method, parent_domain, cookie_domain_patterns, display_name) VALUES
        (NULL, 'google.com', 'direct', NULL, '[".google.com","accounts.google.com","myaccount.google.com","mail.google.com","drive.google.com"]'::jsonb, 'Google'),
        (NULL, 'v0.dev', 'google_oauth', 'google.com', '[".v0.dev","v0.dev"]'::jsonb, 'Vercel v0'),
        (NULL, 'tailscale.com', 'google_oauth', 'google.com', '[".tailscale.com","login.tailscale.com"]'::jsonb, 'Tailscale'),
        (NULL, 'github.com', 'direct', NULL, '[".github.com","github.com"]'::jsonb, 'GitHub'),
        (NULL, 'vercel.com', 'github_oauth', 'github.com', '[".vercel.com","vercel.com"]'::jsonb, 'Vercel'),
        (NULL, 'proton.me', 'direct', NULL, '[".proton.me","proton.me","protonmail.com",".protonmail.com"]'::jsonb, 'Proton');
    """)

def downgrade():
    op.execute("""
    DROP TABLE IF EXISTS domain_registrations;
    """)
