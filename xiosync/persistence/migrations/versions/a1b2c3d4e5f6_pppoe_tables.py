"""Add PPPoE hosts, exit nodes, and fingerprint profiles for XIOGRID.

Revision ID: a1b2c3d4e5f6
Revises: 76bae7a1a189
Create Date: 2026-09-11

Adds:
  - xiogrid_pppoe_hosts         (one Mac Mini + VM pair per row)
  - xiogrid_fingerprint_profiles (8 device profiles, global)
  - xiogrid_pppoe_exit_nodes     (ppp0-ppp980 per host)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = '76bae7a1a189'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. fingerprint profiles (no FK dependencies)
    op.create_table(
        'xiogrid_fingerprint_profiles',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(120), nullable=False, unique=True),
        sa.Column('os', sa.String(20), nullable=False),
        sa.Column('cores', sa.Integer(), nullable=False),
        sa.Column('ram_gb', sa.Integer(), nullable=False),
        sa.Column('webgl_vendor', sa.String(200), nullable=False),
        sa.Column('webgl_renderer', sa.String(200), nullable=False),
        sa.Column('platform', sa.String(40), nullable=False),
        sa.Column('ch_platform', sa.String(40), nullable=False),
        sa.Column('ch_version', sa.String(20), nullable=False),
        sa.Column('ch_arch', sa.String(10), nullable=False),
        sa.Column('screen_width', sa.Integer(), nullable=False),
        sa.Column('screen_height', sa.Integer(), nullable=False),
        sa.Column('dpr', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('cam_name', sa.String(100), nullable=False),
        sa.Column('is_mobile', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('ua_template', sa.Text(), nullable=False),
        sa.Column('canvas_seed', sa.Integer(), nullable=False),
        sa.Column('audio_seed', sa.Integer(), nullable=False),
    )

    # 2. pppoe hosts (one per Mac Mini)
    op.create_table(
        'xiogrid_pppoe_hosts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(120), nullable=False, unique=True),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('vm_ssh_user', sa.String(60), nullable=False, server_default='karmantu'),
        sa.Column('vm_ssh_host', sa.String(120), nullable=False),
        sa.Column('vm_ssh_port', sa.Integer(), nullable=False, server_default='22'),
        sa.Column('vm_scripts_dir', sa.String(200), nullable=False,
                  server_default='/usr/local/bin/xiogrid'),
        sa.Column('pppoe_parent_iface', sa.String(30), nullable=False, server_default='enp26s0'),
        sa.Column('pppoe_username', sa.String(200), nullable=False),
        sa.Column('pppoe_password', sa.String(200), nullable=False),
        sa.Column('mac_oui_prefix', sa.String(20), nullable=False, server_default='00:50:56:cc'),
        sa.Column('tailscale_vm_ts_ip', sa.String(45), nullable=True),
        sa.Column('max_slots', sa.Integer(), nullable=False, server_default='981'),
        sa.Column('warm_pool_target', sa.Integer(), nullable=False, server_default='50'),
        sa.Column('state', sa.String(20), nullable=False, server_default='active'),
        sa.Column('registered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=True),
        sa.Column('meta', postgresql.JSONB(), nullable=False, server_default='{}'),
    )

    # 3. exit nodes (linked to host + fingerprint)
    op.create_table(
        'xiogrid_pppoe_exit_nodes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('host_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('xiogrid_pppoe_hosts.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('fingerprint_profile_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('xiogrid_fingerprint_profiles.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('ppp_slot', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(20), nullable=False, server_default='down'),
        sa.Column('public_ip', sa.String(45), nullable=True),
        sa.Column('cgnat_ip', sa.String(45), nullable=True),
        sa.Column('assigned_worker_ts_ip', sa.String(45), nullable=True),
        sa.Column('assigned_session_id', sa.String(200), nullable=True),
        sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_health_check', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_speed_mbps', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('reconnect_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_sessions_served', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('meta', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.UniqueConstraint('host_id', 'ppp_slot', name='uq_host_ppp_slot'),
    )
    op.create_index('ix_pppoe_nodes_state', 'xiogrid_pppoe_exit_nodes', ['state'])
    op.create_index('ix_pppoe_nodes_host_state', 'xiogrid_pppoe_exit_nodes', ['host_id', 'state'])


def downgrade() -> None:
    op.drop_index('ix_pppoe_nodes_host_state', table_name='xiogrid_pppoe_exit_nodes')
    op.drop_index('ix_pppoe_nodes_state', table_name='xiogrid_pppoe_exit_nodes')
    op.drop_table('xiogrid_pppoe_exit_nodes')
    op.drop_table('xiogrid_pppoe_hosts')
    op.drop_table('xiogrid_fingerprint_profiles')
