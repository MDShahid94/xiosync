"""Migration 0031 — Worker self-enroll support: add last_seen_at + capabilities.

worker_enrollments:
  last_seen_at      TIMESTAMP  — updated by POST /workers/{id}/heartbeat
  tailscale_ip      TEXT       — worker's Tailscale IP, reported at enroll / heartbeat
  reported_caps     JSONB      — capabilities reported by the worker itself
  runtime_type      TEXT       — 'colab' | 'vm' | 'mac' | 'docker'

Revision ID: 0031
Revises: 0030
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_enrollments",
        sa.Column("last_seen_at", pg.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("worker_enrollments",
        sa.Column("tailscale_ip", sa.Text, nullable=True))
    op.add_column("worker_enrollments",
        sa.Column("reported_caps", pg.JSONB, nullable=True))
    op.add_column("worker_enrollments",
        sa.Column("runtime_type", sa.Text, nullable=True))
    op.create_index("ix_worker_enrollments_last_seen",
        "worker_enrollments", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_enrollments_last_seen", "worker_enrollments")
    for col in ("runtime_type", "reported_caps", "tailscale_ip", "last_seen_at"):
        op.drop_column("worker_enrollments", col)
