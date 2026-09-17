"""Add proxy_port, proxy_url, proxy_state to pppoe_exit_nodes

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa

revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('xiogrid_pppoe_exit_nodes',
        sa.Column('proxy_port', sa.Integer(), nullable=True))
    op.add_column('xiogrid_pppoe_exit_nodes',
        sa.Column('proxy_url', sa.String(100), nullable=True))
    op.add_column('xiogrid_pppoe_exit_nodes',
        sa.Column('proxy_state', sa.String(20), nullable=True))

def downgrade() -> None:
    op.drop_column('xiogrid_pppoe_exit_nodes', 'proxy_state')
    op.drop_column('xiogrid_pppoe_exit_nodes', 'proxy_url')
    op.drop_column('xiogrid_pppoe_exit_nodes', 'proxy_port')
