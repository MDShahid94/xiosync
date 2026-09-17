"""0026 — Placeholder slot (no schema changes).

Revision ID: 0026
Revises: 0025

This migration exists solely to fill the numeric gap between 0025 and
``5c2c0ef8c0d1`` (organization_branding) which was originally given a hex
revision ID instead of the sequential ``0026`` slug.  No schema changes are
made here — the ``5c2c0ef8c0d1`` migration that follows this one contains
the actual organisation branding columns.

Safe to run on existing databases: upgrade/downgrade are both no-ops.
"""
from alembic import op  # noqa: F401 — required by Alembic runner

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: this revision is a numeric placeholder only."""


def downgrade() -> None:
    """No-op: this revision is a numeric placeholder only."""
