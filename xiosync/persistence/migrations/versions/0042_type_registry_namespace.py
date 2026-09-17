"""0042 — Update type_registry rows from namespace='xiobr' to namespace='xiosync'.

The bootstrap.py now seeds with namespace='xiosync' for new installs.
This migration updates the 81 existing rows seeded during XIOBR-era setup.
"""
from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "UPDATE type_registry SET namespace='xiosync', updated_at=now() WHERE namespace='xiobr'"
    ))

def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "UPDATE type_registry SET namespace='xiobr', updated_at=now() WHERE namespace='xiosync'"
    ))
