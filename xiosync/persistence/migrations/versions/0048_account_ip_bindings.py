"""0048 — xiogrid_account_ip_bindings (per-account sticky PPPoE slot).

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-19

Each Google account is bound to a specific PPPoE slot so it always exits
via the same residential IP across sessions. This prevents Google from seeing
an IP change between logins, which triggers identity verification prompts.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision  = "0048"
down_revision = "0047"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.create_table(
        "xiogrid_account_ip_bindings",
        sa.Column("id",               sa.Uuid(),   nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id",  sa.Uuid(),   nullable=False),
        sa.Column("google_account",   sa.Text(),   nullable=False),   # e.g. "etathyaghar@gmail.com"
        sa.Column("host_id",          sa.Uuid(),   nullable=False),
        sa.Column("ppp_slot",         sa.Integer(),nullable=False),
        sa.Column("bound_at",         sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_used_at",     sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_sessions",   sa.Integer(),nullable=False, server_default="0"),
        # Optional: store last known public IP for quick display
        sa.Column("last_public_ip",   sa.Text(),   nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_xiogrid_account_ip_bindings"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_account_ip_bindings_org", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["host_id"], ["xiogrid_pppoe_hosts.id"],
            name="fk_account_ip_bindings_host", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id", "google_account",
            name="uq_account_ip_binding_per_org",
        ),
    )
    op.create_index(
        "ix_account_ip_bindings_lookup",
        "xiogrid_account_ip_bindings",
        ["organization_id", "google_account"],
    )
    op.create_index(
        "ix_account_ip_bindings_slot",
        "xiogrid_account_ip_bindings",
        ["host_id", "ppp_slot"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_ip_bindings_slot",   table_name="xiogrid_account_ip_bindings")
    op.drop_index("ix_account_ip_bindings_lookup",  table_name="xiogrid_account_ip_bindings")
    op.drop_table("xiogrid_account_ip_bindings")
