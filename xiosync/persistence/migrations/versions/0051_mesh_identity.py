"""0051 — Mesh node identity independence.

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-21

Stable serial for mesh nodes, Colab account → mesh node binding,
and reform of account_ip_bindings to use identity_id.
"""

from __future__ import annotations

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    -- Stable serial for mesh nodes
    CREATE SEQUENCE IF NOT EXISTS mesh_node_serial_seq START 1;
    ALTER TABLE mesh_nodes ADD COLUMN IF NOT EXISTS serial BIGINT UNIQUE DEFAULT nextval('mesh_node_serial_seq');
    ALTER TABLE mesh_nodes ADD COLUMN IF NOT EXISTS ts_state_object_key TEXT;
    ALTER TABLE mesh_nodes ADD COLUMN IF NOT EXISTS runtime_type TEXT;

    -- Colab account → mesh node binding
    CREATE TABLE mesh_node_bindings (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        organization_id UUID NOT NULL REFERENCES organizations(id),
        colab_account TEXT NOT NULL,
        mesh_node_id UUID NOT NULL REFERENCES mesh_nodes(id),
        serial BIGINT NOT NULL,
        ts_auth_key TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at TIMESTAMPTZ,
        UNIQUE (organization_id, colab_account)
    );

    ALTER TABLE mesh_node_bindings ENABLE ROW LEVEL SECURITY;
    CREATE POLICY rls_mesh_node_bindings ON mesh_node_bindings
        USING (organization_id = current_setting('app.current_org_id')::UUID);

    -- Reform account_ip_bindings: add identity_id, make google_account nullable
    ALTER TABLE xiogrid_account_ip_bindings ADD COLUMN IF NOT EXISTS identity_id UUID REFERENCES identities(id);
    ALTER TABLE xiogrid_account_ip_bindings ALTER COLUMN google_account DROP NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_aib_identity ON xiogrid_account_ip_bindings(organization_id, identity_id) WHERE identity_id IS NOT NULL;
    """)


def downgrade():
    op.execute("""
    DROP INDEX IF EXISTS idx_aib_identity;
    ALTER TABLE xiogrid_account_ip_bindings ALTER COLUMN google_account SET NOT NULL;
    ALTER TABLE xiogrid_account_ip_bindings DROP COLUMN IF EXISTS identity_id;
    
    DROP TABLE IF EXISTS mesh_node_bindings;
    
    ALTER TABLE mesh_nodes DROP COLUMN IF EXISTS runtime_type;
    ALTER TABLE mesh_nodes DROP COLUMN IF EXISTS ts_state_object_key;
    ALTER TABLE mesh_nodes DROP COLUMN IF EXISTS serial;
    DROP SEQUENCE IF EXISTS mesh_node_serial_seq;
    """)
