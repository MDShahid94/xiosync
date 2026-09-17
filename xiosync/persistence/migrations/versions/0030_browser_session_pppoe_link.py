"""Migration 0030 — Link browser_sessions to PPPoE exit nodes.

Adds residential IP identity columns to browser_sessions:
  pppoe_exit_node_id  FK → xiogrid_pppoe_exit_nodes.id
  pppoe_host_id       UUID (denorm — needed for release_from_worker call)
  pppoe_slot          INT  (denorm — needed for release_from_worker call)
  proxy_url           socks5://host:port URL for the session's exit node
  public_ip           residential public IP assigned at acquire time
  worker_ts_ip        Tailscale IP of the worker that owns this session

Revision ID: 0030
Revises: 0029
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("browser_sessions",
        sa.Column("pppoe_exit_node_id", pg.UUID(as_uuid=True), nullable=True))
    op.add_column("browser_sessions",
        sa.Column("pppoe_host_id", pg.UUID(as_uuid=True), nullable=True))
    op.add_column("browser_sessions",
        sa.Column("pppoe_slot", sa.Integer, nullable=True))
    op.add_column("browser_sessions",
        sa.Column("proxy_url", sa.Text, nullable=True))
    op.add_column("browser_sessions",
        sa.Column("public_ip", sa.Text, nullable=True))
    op.add_column("browser_sessions",
        sa.Column("worker_ts_ip", sa.Text, nullable=True))

    op.create_foreign_key(
        "fk_browser_sessions_pppoe_exit_node",
        "browser_sessions", "xiogrid_pppoe_exit_nodes",
        ["pppoe_exit_node_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_browser_sessions_pppoe_exit_node",
        "browser_sessions", ["pppoe_exit_node_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_browser_sessions_pppoe_exit_node", "browser_sessions")
    op.drop_constraint("fk_browser_sessions_pppoe_exit_node", "browser_sessions")
    for col in ("worker_ts_ip", "public_ip", "proxy_url", "pppoe_slot",
                "pppoe_host_id", "pppoe_exit_node_id"):
        op.drop_column("browser_sessions", col)
