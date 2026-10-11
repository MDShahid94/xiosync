"""Add filter_expression to event triggers.

Revision ID: 0063_trigger_filters
Revises: 0062_dag_relational_edges
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0063_trigger_filters"
down_revision = "0062_dag_relational_edges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "xioflow_triggers",
        sa.Column("filter_expression", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("xioflow_triggers", "filter_expression")
