"""Add version and superseded_by to edges for append-only versioning.

Revision ID: 0060_edge_versioning
Revises: 0059_federated_credentials
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0060_edge_versioning"
down_revision = "0059_federated_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("edges", sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")))
    op.add_column("edges", sa.Column("superseded_by", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_edges_superseded_by_same_org", "edges", "edges",
        ["organization_id", "superseded_by"], ["organization_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_edges_superseded_by_same_org", "edges")
    op.drop_column("edges", "superseded_by")
    op.drop_column("edges", "version")
