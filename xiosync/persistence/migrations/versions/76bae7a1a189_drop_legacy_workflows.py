"""Drop legacy workflows

Revision ID: 76bae7a1a189
Revises: 0027
Create Date: 2026-09-04 01:31:09.957640

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '76bae7a1a189'
down_revision: Union[str, None] = '0027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("workflow_triggers")
    op.drop_table("dead_letters")
    op.drop_table("tasks")
    op.drop_table("workflow_runs")
    op.drop_table("workflows")

def downgrade() -> None:
    pass
