"""0052 — Domain proxy rules and workflow network scopes.

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-21

Domain-specific proxy rules (Layer 3) and workflow network scopes (Layer 4)
for the hierarchical network binding system.
"""
from __future__ import annotations

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""
    CREATE TABLE domain_proxy_rules (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        organization_id UUID NOT NULL REFERENCES organizations(id),
        identity_id UUID NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
        domain_pattern TEXT NOT NULL,
        host_id UUID,
        ppp_slot INTEGER,
        proxy_url TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (organization_id, identity_id, domain_pattern)
    );

    CREATE TABLE workflow_network_scopes (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        organization_id UUID NOT NULL REFERENCES organizations(id),
        workflow_run_id UUID NOT NULL,
        dedicated_slot INTEGER,
        host_id UUID,
        proxy_url TEXT NOT NULL,
        slot_policy TEXT NOT NULL DEFAULT 'exclusive',
        acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        released_at TIMESTAMPTZ,
        UNIQUE (organization_id, workflow_run_id)
    );

    ALTER TABLE domain_proxy_rules ENABLE ROW LEVEL SECURITY;
    ALTER TABLE workflow_network_scopes ENABLE ROW LEVEL SECURITY;
    CREATE POLICY rls_domain_proxy_rules ON domain_proxy_rules
        USING (organization_id = current_setting('app.current_org_id')::UUID);
    CREATE POLICY rls_workflow_network_scopes ON workflow_network_scopes
        USING (organization_id = current_setting('app.current_org_id')::UUID);
    """)

def downgrade():
    op.execute("""
    DROP TABLE IF EXISTS workflow_network_scopes;
    DROP TABLE IF EXISTS domain_proxy_rules;
    """)
