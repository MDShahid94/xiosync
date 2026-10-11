"""Add properties JSONB to edges table.

Revision ID: 0058_edge_properties
Revises: 0057_grant_delegation
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0058_edge_properties"
down_revision = "0057_grant_delegation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "edges",
        sa.Column("properties", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_check_constraint(
        "ck_edges_properties_object", "edges",
        sa.text("jsonb_typeof(properties) = 'object'"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_edges_properties_object", "edges")
    op.drop_column("edges", "properties")
