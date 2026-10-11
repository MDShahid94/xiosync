"""0055 — IPv6 dual-stack columns for XIOGRID models.

Revision ID: 0055_ipv6_dual_stack
Revises: 0054_xioflow_agents
Create Date: 2026-10-11

Adds IPv6 columns to support dual-stack networking:
  - xiogrid_pppoe_hosts: tailscale_vm_ts_ipv6
  - xiogrid_pppoe_exit_nodes: public_ipv6, ipv6_prefix, assigned_worker_ts_ipv6, proxy_url_v6
  - xiogrid_account_ip_bindings: last_public_ipv6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_ipv6_dual_stack"
down_revision = "0054_xioflow_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. xiogrid_pppoe_hosts ─────────────────────────────────────────────
    op.add_column(
        "xiogrid_pppoe_hosts",
        sa.Column("tailscale_vm_ts_ipv6", sa.String(45), nullable=True),
    )

    # ── 2. xiogrid_pppoe_exit_nodes ────────────────────────────────────────
    op.add_column(
        "xiogrid_pppoe_exit_nodes",
        sa.Column("public_ipv6", sa.String(45), nullable=True),
    )
    op.add_column(
        "xiogrid_pppoe_exit_nodes",
        sa.Column("ipv6_prefix", sa.String(50), nullable=True),
    )
    op.add_column(
        "xiogrid_pppoe_exit_nodes",
        sa.Column("assigned_worker_ts_ipv6", sa.String(45), nullable=True),
    )
    op.add_column(
        "xiogrid_pppoe_exit_nodes",
        sa.Column("proxy_url_v6", sa.String(120), nullable=True),
    )

    # ── 3. xiogrid_account_ip_bindings ────────────────────────────────────
    op.add_column(
        "xiogrid_account_ip_bindings",
        sa.Column("last_public_ipv6", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    # ── 3. xiogrid_account_ip_bindings ────────────────────────────────────
    op.drop_column("xiogrid_account_ip_bindings", "last_public_ipv6")

    # ── 2. xiogrid_pppoe_exit_nodes ────────────────────────────────────────
    op.drop_column("xiogrid_pppoe_exit_nodes", "proxy_url_v6")
    op.drop_column("xiogrid_pppoe_exit_nodes", "assigned_worker_ts_ipv6")
    op.drop_column("xiogrid_pppoe_exit_nodes", "ipv6_prefix")
    op.drop_column("xiogrid_pppoe_exit_nodes", "public_ipv6")

    # ── 1. xiogrid_pppoe_hosts ─────────────────────────────────────────────
    op.drop_column("xiogrid_pppoe_hosts", "tailscale_vm_ts_ipv6")
