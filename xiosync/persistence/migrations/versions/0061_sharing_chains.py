"""Add parent_share_id to resource_shares for cascaded revocation.

Revision ID: 0061_sharing_chains
Revises: 0060_edge_versioning
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0061_sharing_chains"
down_revision = "0060_edge_versioning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resource_shares",
        sa.Column("parent_share_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_resource_shares_parent",
        "resource_shares",
        "resource_shares",
        ["parent_share_id"],
        ["id"],
    )
    op.create_index(
        "ix_resource_shares_parent",
        "resource_shares",
        ["parent_share_id"],
        postgresql_where=sa.text("parent_share_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_resource_shares_parent", "resource_shares")
    op.drop_constraint("fk_resource_shares_parent", "resource_shares")
    op.drop_column("resource_shares", "parent_share_id")
