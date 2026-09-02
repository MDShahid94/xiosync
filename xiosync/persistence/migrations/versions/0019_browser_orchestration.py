"""0019 — browser orchestration tables for XIOBR decoupling.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_ts = TIMESTAMP(timezone=True)

# Register new types via bootstrap:
_SEED_CATEGORIES = [
    ("browser_engine", "xiobr", "Valid browser engine types"),
    ("mesh_provider", "xiobr", "Valid mesh network providers"),
]


def upgrade() -> None:
    # 1. Seed categories for type_registry
    from uuid import uuid4
    cat_rows = [
        {"id": str(uuid4()), "name": name, "namespace": ns, "description": desc}
        for name, ns, desc in _SEED_CATEGORIES
    ]
    op.bulk_insert(
        sa.table(
            "registry_categories",
            sa.column("id", UUID(as_uuid=True)),
            sa.column("name", sa.Text()),
            sa.column("namespace", sa.Text()),
            sa.column("description", sa.Text()),
        ),
        cat_rows,
    )

    # 2. compute_runtimes
    op.create_table(
        "compute_runtimes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_at", _ts, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _ts),
        sa.CheckConstraint("state IN ('active', 'offline', 'deprecated')", name="ck_compute_runtimes_state"),
        sa.UniqueConstraint("organization_id", "name", name="uq_compute_runtimes_org_name"),
    )
    
    # 3. runtime_nodes
    op.create_table(
        "runtime_nodes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("runtime_id", UUID(as_uuid=True), sa.ForeignKey("compute_runtimes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("hostname", sa.Text, nullable=False),
        sa.Column("ip_address", sa.Text),
        sa.Column("node_metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.Text, nullable=False, server_default="provisioning"),
        sa.Column("created_at", _ts, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _ts),
        sa.CheckConstraint("state IN ('provisioning', 'ready', 'busy', 'offline', 'terminated')", name="ck_runtime_nodes_state"),
        sa.Index("ix_runtime_nodes_runtime", "runtime_id"),
    )

    # 4. browser_pools
    op.create_table(
        "browser_pools",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("engine_type", sa.Text, nullable=False, server_default="chromium"),
        sa.Column("max_instances", sa.Integer, nullable=False, server_default="10"),
        sa.Column("stealth_config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_at", _ts, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _ts),
        sa.CheckConstraint("state IN ('active', 'suspended', 'archived')", name="ck_browser_pools_state"),
        sa.UniqueConstraint("organization_id", "name", name="uq_browser_pools_org_name"),
    )

    # 5. browser_sessions
    op.create_table(
        "browser_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("pool_id", UUID(as_uuid=True), sa.ForeignKey("browser_pools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", UUID(as_uuid=True), sa.ForeignKey("runtime_nodes.id")),
        sa.Column("session_data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.Text, nullable=False, server_default="initializing"),
        sa.Column("created_at", _ts, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _ts),
        sa.CheckConstraint("state IN ('initializing', 'running', 'closing', 'completed', 'failed')", name="ck_browser_sessions_state"),
        sa.Index("ix_browser_sessions_pool", "pool_id"),
    )

    # 6. mesh_networks
    op.create_table(
        "mesh_networks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("network_type", sa.Text, nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.Text, nullable=False, server_default="configuring"),
        sa.Column("created_at", _ts, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _ts),
        sa.CheckConstraint("state IN ('active', 'configuring', 'error', 'disabled')", name="ck_mesh_networks_state"),
        sa.UniqueConstraint("organization_id", "name", name="uq_mesh_networks_org_name"),
    )

    # RLS policies
    for table in ["compute_runtimes", "runtime_nodes", "browser_pools", "browser_sessions", "mesh_networks"]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation_{table} ON {table}
            USING (organization_id = current_setting('app.current_org_id')::uuid)
        """)


def downgrade() -> None:
    for table in ["mesh_networks", "browser_sessions", "browser_pools", "runtime_nodes", "compute_runtimes"]:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.drop_table(table)
    
    op.execute(
        f"DELETE FROM registry_categories WHERE name IN ({', '.join(repr(name) for name, _, _ in _SEED_CATEGORIES)})"
    )
