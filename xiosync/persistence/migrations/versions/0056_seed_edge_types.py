"""Seed well-known edge types into type_registry.

Revision ID: 0056_seed_edge_types
Revises: 0055_ipv6_dual_stack
"""

from alembic import op
from sqlalchemy import text

revision = "0056_seed_edge_types"
down_revision = "0055_ipv6_dual_stack"
branch_labels = None
depends_on = None

_EDGE_TYPES = [
    ("authenticates_as", "Identity authenticates with external service"),
    ("materializes_to", "Identity profile stored as object"),
    ("routes_through", "Identity traffic exits through PPPoE slot"),
    ("runs_on", "Mesh node runs on compute runtime"),
    ("depends_on_auth", "Domain auth depends on parent domain"),
    ("binds_network", "Workflow run bound to network scope"),
]


def upgrade() -> None:
    for value, description in _EDGE_TYPES:
        op.execute(
            text(
                "INSERT INTO type_registry (id, namespace, category, value, version, state, definition) "
                "VALUES (gen_random_uuid(), 'core', 'edge_type', :value, 1, 'active', "
                "jsonb_build_object('description', :desc)) "
                "ON CONFLICT (namespace, category, value, version) DO NOTHING"
            ).bindparams(value=value, desc=description)
        )


def downgrade() -> None:
    for value, _ in _EDGE_TYPES:
        op.execute(
            text(
                "DELETE FROM type_registry WHERE namespace = 'core' "
                "AND category = 'edge_type' AND value = :value"
            ).bindparams(value=value)
        )
