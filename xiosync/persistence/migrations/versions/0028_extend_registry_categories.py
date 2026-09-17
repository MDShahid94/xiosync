"""Add trigger_type and plugin_type to type_registry category check constraint.

Bootstrap seeds entries for trigger_type (cron, event, webhook) and plugin_type
but these were missing from the original category_allowed check constraint
defined in migration 0005 / revised in 0017.

Revision ID: 0028_extend_registry_categories
Revises:     0027_xioflow_memory_tables
"""
from __future__ import annotations

from alembic import op

revision = "0028_extend_registry_categories"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None

# Extended set of allowed categories
_ALLOWED = (
    "actor_type",
    "actor_subtype",
    "capability_type",
    "operation_type",
    "edge_type",
    "event_type",
    "lifecycle_state",
    "trigger_type",    # ← added: cron / event / webhook
    "plugin_type",     # ← added: for future plugin category registry
    "registry_category",  # ← added: meta-categories for registry_categories table
)

_CHECK_EXPR = "category = ANY (ARRAY[{}])".format(
    ", ".join(f"'{v}'::text" for v in _ALLOWED)
)


def upgrade() -> None:
    # Drop old constraints on both tables
    op.execute(
        "ALTER TABLE type_registry "
        "DROP CONSTRAINT IF EXISTS ck_type_registry_ck_type_registry_category_allowed"
    )
    op.execute(
        "ALTER TABLE type_registry "
        "DROP CONSTRAINT IF EXISTS category_allowed"
    )
    op.execute(
        "ALTER TABLE type_registry_aliases "
        "DROP CONSTRAINT IF EXISTS ck_type_registry_aliases_ck_type_registry_aliases_category_allowed"
    )
    op.execute(
        "ALTER TABLE type_registry_aliases "
        "DROP CONSTRAINT IF EXISTS category_allowed"
    )

    # Re-add with extended set
    op.execute(
        f"ALTER TABLE type_registry "
        f"ADD CONSTRAINT ck_type_registry_category_allowed CHECK ({_CHECK_EXPR})"
    )
    op.execute(
        f"ALTER TABLE type_registry_aliases "
        f"ADD CONSTRAINT ck_type_registry_aliases_category_allowed CHECK ({_CHECK_EXPR})"
    )


def downgrade() -> None:
    # Restore original narrow set
    _OLD_ALLOWED = (
        "actor_type", "actor_subtype", "capability_type",
        "operation_type", "edge_type", "event_type", "lifecycle_state",
    )
    _OLD_EXPR = "category = ANY (ARRAY[{}])".format(
        ", ".join(f"'{v}'::text" for v in _OLD_ALLOWED)
    )
    op.execute(
        "ALTER TABLE type_registry "
        "DROP CONSTRAINT IF EXISTS ck_type_registry_category_allowed"
    )
    op.execute(
        f"ALTER TABLE type_registry "
        f"ADD CONSTRAINT ck_type_registry_category_allowed CHECK ({_OLD_EXPR})"
    )
    op.execute(
        "ALTER TABLE type_registry_aliases "
        "DROP CONSTRAINT IF EXISTS ck_type_registry_aliases_category_allowed"
    )
    op.execute(
        f"ALTER TABLE type_registry_aliases "
        f"ADD CONSTRAINT ck_type_registry_aliases_category_allowed CHECK ({_OLD_EXPR})"
    )
