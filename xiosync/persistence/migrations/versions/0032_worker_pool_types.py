"""Migration 0032 — Broaden worker_enrollments.pool_type.

Revision ID: 0032
Revises: 0031
"""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("ALTER TABLE worker_enrollments DROP CONSTRAINT IF EXISTS ck_worker_enrollments_pool_type_allowed")
    op.execute("""
        ALTER TABLE worker_enrollments ADD CONSTRAINT ck_worker_enrollments_pool_type_allowed
        CHECK (pool_type IN ('managed','volunteer','colab','vm','mac','docker','ephemeral','spot','dedicated'))
    """)

def downgrade() -> None:
    op.execute("ALTER TABLE worker_enrollments DROP CONSTRAINT IF EXISTS ck_worker_enrollments_pool_type_allowed")
    op.execute("ALTER TABLE worker_enrollments ADD CONSTRAINT ck_worker_enrollments_pool_type_allowed CHECK (pool_type IN ('managed','volunteer'))")
