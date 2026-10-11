"""Create relational tables for DAG workflow nodes and edges.

Revision ID: 0062_dag_relational_edges
Revises: 0061_sharing_chains
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0062_dag_relational_edges"
down_revision = "0061_sharing_chains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "xioflow_dag_nodes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("template_id", UUID(as_uuid=True), sa.ForeignKey("workflow_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_key", sa.Text, nullable=False),
        sa.Column("node_type", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=True),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("position_x", sa.Float, nullable=True),
        sa.Column("position_y", sa.Float, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("template_id", "node_key", name="uq_dag_node_key_per_template"),
    )
    op.create_index("ix_dag_nodes_template", "xioflow_dag_nodes", ["template_id"])

    op.create_table(
        "xioflow_dag_edges",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("template_id", UUID(as_uuid=True), sa.ForeignKey("workflow_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_node_id", UUID(as_uuid=True), sa.ForeignKey("xioflow_dag_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_node_id", UUID(as_uuid=True), sa.ForeignKey("xioflow_dag_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("condition", JSONB, nullable=True, doc="Branch condition expression"),
        sa.Column("edge_label", sa.Text, nullable=True),
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("template_id", "source_node_id", "target_node_id", name="uq_dag_edge_per_template"),
    )
    op.create_index("ix_dag_edges_template", "xioflow_dag_edges", ["template_id"])
    op.create_index("ix_dag_edges_source", "xioflow_dag_edges", ["source_node_id"])
    op.create_index("ix_dag_edges_target", "xioflow_dag_edges", ["target_node_id"])


def downgrade() -> None:
    op.drop_table("xioflow_dag_edges")
    op.drop_table("xioflow_dag_nodes")
