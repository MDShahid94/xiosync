"""Sandboxed-plugin tables. Revision ID: 0009; Revises: 0008.

Creates the Phase 5 plugin subsystem tables (doc 07 §5; DECISIONS.md D-008):

* ``plugins`` — registered plugin manifests: identity, launch entrypoint, the
  required-capability Grant (composite same-org FK), resource quota, filesystem
  jail, and manifest hash (INV-PLUGIN-1).
* ``plugin_rpc_methods`` — typed host↔plugin RPC method contracts with
  JSON-Schema input/output (INV-PLUGIN-2).
* ``plugin_installations`` — approval-gated installs; land ``pending_approval``
  (INV-PLUGIN-3), link the minted capability Grant on approval.
* ``plugin_network_allow_rules`` — explicit per-installation network allowlist;
  no rows = deny all, and there is no allow-all row (INV-PLUGIN-4).

Structural DDL mirrors the ORM metadata in ``persistence/models/plugins`` (what
autogenerate emits and the INV-TEST-SCHEMA-2 diff gate compares against). RLS
org-isolation follows the pattern set by revisions 0004–0008.

Reversibility (INV-MIG-3): downgrade drops all four tables in FK-safe order.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None

# Standard tenant isolation GUC check (mirrors revisions 0004–0008).
_ORG_ISOLATION = (
    "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # plugins
    # ------------------------------------------------------------------
    op.create_table(
        "plugins",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("entrypoint", sa.Text(), nullable=False),
        sa.Column(
            "required_capability_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("cpu_millis", sa.Integer(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("filesystem_jail", sa.Text(), nullable=False),
        sa.Column("manifest_hash", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'registered'"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugins")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_plugins_organization_id_organizations"),
        ),
        # INV-PLUGIN-1: required capability is a capability in the same org.
        sa.ForeignKeyConstraint(
            ["organization_id", "required_capability_id"],
            ["capabilities.organization_id", "capabilities.id"],
            name="fk_plugins_required_capability_same_org",
        ),
        # Anchor for rpc-method / installation composite FKs.
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name=op.f("uq_plugins_organization_id_id"),
        ),
        sa.UniqueConstraint(
            "organization_id",
            "name",
            "version",
            name="uq_plugins_organization_id_name_version",
        ),
        sa.CheckConstraint(
            "state IN ('registered', 'deprecated')",
            name="state_allowed",
        ),
        sa.CheckConstraint("cpu_millis > 0", name="cpu_millis_positive"),
        sa.CheckConstraint("memory_mb > 0", name="memory_mb_positive"),
        sa.CheckConstraint("timeout_seconds > 0", name="timeout_seconds_positive"),
    )
    op.create_index(
        op.f("ix_plugins_organization_id"),
        "plugins",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugins_org_state",
        "plugins",
        ["organization_id", "state"],
        unique=False,
    )
    op.execute("ALTER TABLE plugins ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY plugins_org_isolation
            ON plugins
            USING ({_ORG_ISOLATION})
        """
    )

    # ------------------------------------------------------------------
    # plugin_rpc_methods
    # ------------------------------------------------------------------
    op.create_table(
        "plugin_rpc_methods",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plugin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("method_name", sa.Text(), nullable=False),
        sa.Column(
            "input_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "output_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_rpc_methods")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_plugin_rpc_methods_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "plugin_id"],
            ["plugins.organization_id", "plugins.id"],
            name="fk_plugin_rpc_methods_plugin_same_org",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "plugin_id",
            "method_name",
            name="uq_plugin_rpc_methods_org_plugin_method",
        ),
    )
    op.create_index(
        op.f("ix_plugin_rpc_methods_organization_id"),
        "plugin_rpc_methods",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_rpc_methods_org_plugin",
        "plugin_rpc_methods",
        ["organization_id", "plugin_id"],
        unique=False,
    )
    op.execute("ALTER TABLE plugin_rpc_methods ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY plugin_rpc_methods_org_isolation
            ON plugin_rpc_methods
            USING ({_ORG_ISOLATION})
        """
    )

    # ------------------------------------------------------------------
    # plugin_installations
    # ------------------------------------------------------------------
    op.create_table(
        "plugin_installations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plugin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending_approval'"),
        ),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "requested_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "approved_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_installations")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_plugin_installations_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "plugin_id"],
            ["plugins.organization_id", "plugins.id"],
            name="fk_plugin_installations_plugin_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "requested_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_plugin_installations_requested_by_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "approved_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_plugin_installations_approved_by_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "grant_id"],
            ["grants.organization_id", "grants.id"],
            name="fk_plugin_installations_grant_same_org",
        ),
        # Anchor for the network-allow-rule composite FK.
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name=op.f("uq_plugin_installations_organization_id_id"),
        ),
        sa.UniqueConstraint(
            "organization_id",
            "plugin_id",
            name="uq_plugin_installations_org_plugin",
        ),
        sa.CheckConstraint(
            "state IN ('pending_approval', 'approved', 'active', 'suspended', 'revoked')",
            name="state_allowed",
        ),
    )
    op.create_index(
        op.f("ix_plugin_installations_organization_id"),
        "plugin_installations",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_installations_org_state",
        "plugin_installations",
        ["organization_id", "state"],
        unique=False,
    )
    op.execute("ALTER TABLE plugin_installations ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY plugin_installations_org_isolation
            ON plugin_installations
            USING ({_ORG_ISOLATION})
        """
    )

    # ------------------------------------------------------------------
    # plugin_network_allow_rules
    # ------------------------------------------------------------------
    op.create_table(
        "plugin_network_allow_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_network_allow_rules")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_plugin_network_allow_rules_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "installation_id"],
            ["plugin_installations.organization_id", "plugin_installations.id"],
            name="fk_plugin_network_allow_rules_installation_same_org",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "installation_id",
            "host",
            "port",
            "protocol",
            name="uq_plugin_network_allow_rules_installation_destination",
        ),
        sa.CheckConstraint(
            "protocol IN ('tcp', 'udp', 'http', 'https', 'tls')",
            name="protocol_allowed",
        ),
        sa.CheckConstraint("port >= 1 AND port <= 65535", name="port_range"),
    )
    op.create_index(
        op.f("ix_plugin_network_allow_rules_organization_id"),
        "plugin_network_allow_rules",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_network_allow_rules_org_installation",
        "plugin_network_allow_rules",
        ["organization_id", "installation_id"],
        unique=False,
    )
    op.execute("ALTER TABLE plugin_network_allow_rules ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY plugin_network_allow_rules_org_isolation
            ON plugin_network_allow_rules
            USING ({_ORG_ISOLATION})
        """
    )


def downgrade() -> None:
    # Drop in FK-safe reverse order.
    op.execute(
        "DROP POLICY IF EXISTS plugin_network_allow_rules_org_isolation "
        "ON plugin_network_allow_rules"
    )
    op.drop_index(
        "ix_plugin_network_allow_rules_org_installation",
        table_name="plugin_network_allow_rules",
    )
    op.drop_index(
        op.f("ix_plugin_network_allow_rules_organization_id"),
        table_name="plugin_network_allow_rules",
    )
    op.drop_table("plugin_network_allow_rules")

    op.execute(
        "DROP POLICY IF EXISTS plugin_installations_org_isolation ON plugin_installations"
    )
    op.drop_index(
        "ix_plugin_installations_org_state", table_name="plugin_installations"
    )
    op.drop_index(
        op.f("ix_plugin_installations_organization_id"),
        table_name="plugin_installations",
    )
    op.drop_table("plugin_installations")

    op.execute(
        "DROP POLICY IF EXISTS plugin_rpc_methods_org_isolation ON plugin_rpc_methods"
    )
    op.drop_index(
        "ix_plugin_rpc_methods_org_plugin", table_name="plugin_rpc_methods"
    )
    op.drop_index(
        op.f("ix_plugin_rpc_methods_organization_id"),
        table_name="plugin_rpc_methods",
    )
    op.drop_table("plugin_rpc_methods")

    op.execute("DROP POLICY IF EXISTS plugins_org_isolation ON plugins")
    op.drop_index("ix_plugins_org_state", table_name="plugins")
    op.drop_index(op.f("ix_plugins_organization_id"), table_name="plugins")
    op.drop_table("plugins")
