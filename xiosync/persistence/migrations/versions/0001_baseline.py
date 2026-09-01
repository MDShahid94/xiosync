"""baseline — establish the single linear migration chain (no schema objects)

Revision ID: 0001
Revises: None
Create Date: 2026-07-15

Phase 0 (doc 10) requires the chain to exist and be provably reversible on an
empty database before any table lands. This revision intentionally creates
nothing: it is the chain's root. Tables arrive in Phase 1+ revisions on top of
it (doc 06 §5).

Reversibility (INV-MIG-3): fully reversible (no-op both directions).
"""

from __future__ import annotations

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
