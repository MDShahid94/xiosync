"""0047 — compute_node_definitions table.

Revision ID: 0047
Revises: c3d4e5f6a7b8

Adds ``compute_node_definitions`` table to persist registered compute
nodes in the DB rather than the in-memory dict in compute_nodes.py.

Columns
-------
id                  PK UUID
organization_id     owning org (NULL = platform-global)
name                unique slug per org
display_name        human-friendly name
description         markdown description
runtime             e.g. "python3.11", "node20", "docker"
source_code         the actual code blob
manifest            JSONB schema / input-output declarations
enabled             bool — whether org can use this node
created_at, updated_at
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compute_node_definitions",
        sa.Column("id",              postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name",            sa.Text(), nullable=False),
        sa.Column("display_name",    sa.Text(), nullable=True),
        sa.Column("description",     sa.Text(), nullable=True),
        sa.Column("runtime",         sa.Text(), nullable=False),
        sa.Column("source_code",     sa.Text(), nullable=False),
        sa.Column("manifest",        postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("enabled",         sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at",      sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_compute_node_org_name"),
    )
    op.create_index("ix_compute_node_definitions_org", "compute_node_definitions", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_compute_node_definitions_org", table_name="compute_node_definitions")
    op.drop_table("compute_node_definitions")
