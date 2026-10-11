"""Add delegation provenance to grants + prerequisites to capabilities.

Revision ID: 0057_grant_delegation
Revises: 0056_seed_edge_types
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0057_grant_delegation"
down_revision = "0056_seed_edge_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Grant delegation provenance
    op.add_column("grants", sa.Column("granted_by", UUID(as_uuid=True), nullable=True))
    op.add_column("grants", sa.Column("parent_grant_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_grants_granted_by_actors", "grants", "actors",
        ["granted_by"], ["id"],
    )
    op.create_foreign_key(
        "fk_grants_parent_grant_same_org", "grants", "grants",
        ["organization_id", "parent_grant_id"], ["organization_id", "id"],
    )
    op.create_index(
        "ix_grants_parent_grant_id", "grants", ["parent_grant_id"],
        postgresql_where=sa.text("parent_grant_id IS NOT NULL"),
    )

    # Capability prerequisites
    op.add_column(
        "capabilities",
        sa.Column("requires", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_check_constraint(
        "ck_capabilities_requires_array", "capabilities",
        sa.text("jsonb_typeof(requires) = 'array'"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_capabilities_requires_array", "capabilities")
    op.drop_column("capabilities", "requires")
    op.drop_index("ix_grants_parent_grant_id", "grants")
    op.drop_constraint("fk_grants_parent_grant_same_org", "grants")
    op.drop_constraint("fk_grants_granted_by_actors", "grants")
    op.drop_column("grants", "parent_grant_id")
    op.drop_column("grants", "granted_by")
