"""0021 — mesh_nodes table.

Revision ID: 0021
Revises: 0020

The ``MeshNode`` ORM model (browser.py) was introduced alongside the other
XIOBR tables in migration 0019 but was accidentally omitted from the
corresponding ``op.create_table`` calls.  This migration retrofits the missing
table so the live schema matches the ORM metadata.

It also back-fills the composite unique constraint on ``mesh_networks``
(``uq_mesh_networks_org_id``) that the ORM declares but migration 0019 omitted.
That constraint is a prerequisite for the composite FK from ``mesh_nodes`` →
``mesh_networks``.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_ts = TIMESTAMP(timezone=True)


def upgrade() -> None:
    # 0. Back-fill the missing composite unique on mesh_networks so the FK below
    #    has a valid target (PostgreSQL requires a unique constraint on the
    #    referenced columns).
    op.create_unique_constraint(
        "uq_mesh_networks_org_id",
        "mesh_networks",
        ["organization_id", "id"],
    )

    # 1. Create mesh_nodes
    op.create_table(
        "mesh_nodes",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        # network_id participates in the composite FK; no separate simple FK.
        sa.Column("network_id", UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", UUID(as_uuid=True), nullable=False),
        sa.Column("address", sa.Text, nullable=False),
        sa.Column(
            "created_at", _ts, nullable=False, server_default=sa.text("now()")
        ),
        # Composite unique: org-scoped surrogate for downstream FK chains.
        sa.UniqueConstraint("organization_id", "id", name="uq_mesh_nodes_org_id"),
        # Same-org enforcement: node must belong to a network in the same org.
        sa.ForeignKeyConstraint(
            ["organization_id", "network_id"],
            ["mesh_networks.organization_id", "mesh_networks.id"],
            name="fk_mesh_nodes_network_same_org",
            ondelete="CASCADE",
        ),
    )

    # 2. Row-level security — same pattern as every other XIOBR table.
    op.execute("ALTER TABLE mesh_nodes ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_mesh_nodes ON mesh_nodes
        USING (organization_id = current_setting('app.current_org_id')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_mesh_nodes ON mesh_nodes")
    op.drop_table("mesh_nodes")
    op.drop_constraint("uq_mesh_networks_org_id", "mesh_networks", type_="unique")
