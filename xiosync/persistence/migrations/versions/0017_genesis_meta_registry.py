"""Genesis Phase 0a — meta-registry and capability groups.

Revision ID: 0017
Revises: 0016

Three structural changes:

1. **Meta-registry (Gap G-6)**: Replaces the hardcoded 7-value CHECK constraint
   on ``type_registry.category`` and ``type_registry_aliases.category`` with a
   ``registry_categories`` table.  New concept categories can now be registered
   at runtime without DDL.  The existing 7 categories plus 4 new ones are seeded.

2. **Capability groups (Gap G-3 / Q2-C)**: Creates the ``capability_groups``
   table for configurable RBAC — a named group expands to a list of fine-grained
   capability operations.  Route handlers check against a group; the group
   definition is tenant-configurable.

3. **Bootstrap infrastructure**: Adds columns and constraints needed for the
   Genesis bootstrap service (Phase 0b).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_timestamptz = TIMESTAMP(timezone=True)

# -- The 7 original + 4 new categories ----------------------------------------
_SEED_CATEGORIES = [
    # Original 7
    ("actor_type", "core", "Valid actor type values (human, ai_agent, system, etc.)"),
    ("actor_subtype", "core", "Valid actor subtype values"),
    ("capability_type", "core", "Valid capability type classification values"),
    ("operation_type", "core", "Valid operation type values"),
    ("edge_type", "core", "Valid edge type values for the ontology graph"),
    ("event_type", "core", "Valid event type values for the audit log"),
    ("lifecycle_state", "core", "Valid lifecycle state values for state machines"),
    # New categories enabled by meta-registry
    ("artifact_type", "core", "Content type classification for artifacts"),
    ("trigger_type", "core", "Trigger mechanism types (cron, event, webhook, etc.)"),
    ("share_type", "core", "Shareable resource type categories"),
    ("environment_type", "core", "Execution environment classifications"),
    ("capability_group", "core", "Named capability group definitions for RBAC"),
    ("meta_category", "core", "Registry category definitions (self-referential)"),
]


def upgrade() -> None:
    # ---- 1. Create registry_categories table ---------------------------------
    op.create_table(
        "registry_categories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("namespace", sa.Text(), nullable=False, server_default="core"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "created_at", _timestamptz, nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("name", name=op.f("uq_registry_categories_name")),
        sa.CheckConstraint(
            "state IN ('active', 'deprecated')",
            name=op.f("ck_registry_categories_state_allowed"),
        ),
    )

    # Seed categories
    from uuid import uuid4

    cat_rows = [
        {"id": str(uuid4()), "name": name, "namespace": ns, "description": desc}
        for name, ns, desc in _SEED_CATEGORIES
    ]
    op.bulk_insert(
        sa.table(
            "registry_categories",
            sa.column("id", UUID(as_uuid=True)),
            sa.column("name", sa.Text()),
            sa.column("namespace", sa.Text()),
            sa.column("description", sa.Text()),
        ),
        cat_rows,
    )

    # ---- 2. Drop hardcoded CHECK on type_registry.category -------------------
    # The CHECK was named via op.f() convention: ck_type_registry_category_allowed
    op.execute(
        "ALTER TABLE type_registry DROP CONSTRAINT IF EXISTS "
        "ck_type_registry_category_allowed"
    )
    # Also try the bare name used in models
    op.execute(
        "ALTER TABLE type_registry DROP CONSTRAINT IF EXISTS category_allowed"
    )

    # Drop the same CHECK on type_registry_aliases
    op.execute(
        "ALTER TABLE type_registry_aliases DROP CONSTRAINT IF EXISTS "
        "ck_type_registry_aliases_category_allowed"
    )
    op.execute(
        "ALTER TABLE type_registry_aliases DROP CONSTRAINT IF EXISTS category_allowed"
    )

    # Add FK from type_registry.category → registry_categories.name
    # (deferred constraint so bulk inserts work)
    op.create_foreign_key(
        "fk_type_registry_category",
        "type_registry",
        "registry_categories",
        ["category"],
        ["name"],
    )
    op.create_foreign_key(
        "fk_type_registry_aliases_category",
        "type_registry_aliases",
        "registry_categories",
        ["category"],
        ["name"],
    )

    # ---- 3. Create capability_groups table -----------------------------------
    op.create_table(
        "capability_groups",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # List of fine-grained operation strings this group expands to.
        # e.g. ["workflow.create", "workflow.publish", "workflow.start_run"]
        sa.Column(
            "operations",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("state", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "created_at", _timestamptz, nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("updated_at", _timestamptz, nullable=True),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_capability_groups_org_name")
        ),
        sa.CheckConstraint(
            "state IN ('active', 'deprecated')",
            name=op.f("ck_capability_groups_state_allowed"),
        ),
    )

    # RLS on capability_groups — org-scoped with NULL = global/system
    op.execute("ALTER TABLE capability_groups ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE capability_groups FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON capability_groups
        FOR ALL
        USING (
            organization_id IS NULL
            OR organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid
        )
        WITH CHECK (
            organization_id IS NULL
            OR organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid
        )
    """)

    # ---- 4. RLS on registry_categories (global read, restricted write) -------
    op.execute("ALTER TABLE registry_categories ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE registry_categories FORCE ROW LEVEL SECURITY")
    # Everyone can read categories; write requires superuser/migration context
    op.execute("""
        CREATE POLICY category_read ON registry_categories
        FOR SELECT USING (true)
    """)


def downgrade() -> None:
    # Drop RLS
    op.execute("DROP POLICY IF EXISTS category_read ON registry_categories")
    op.execute("ALTER TABLE registry_categories DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS org_isolation ON capability_groups")
    op.execute("ALTER TABLE capability_groups DISABLE ROW LEVEL SECURITY")

    # Drop capability_groups
    op.drop_table("capability_groups")

    # Drop FKs
    op.drop_constraint("fk_type_registry_aliases_category", "type_registry_aliases")
    op.drop_constraint("fk_type_registry_category", "type_registry")

    # Restore CHECKs
    _category_check = (
        "category IN ("
        "'actor_type', 'actor_subtype', 'capability_type', 'operation_type', "
        "'edge_type', 'event_type', 'lifecycle_state')"
    )
    op.execute(
        f"ALTER TABLE type_registry ADD CONSTRAINT category_allowed CHECK ({_category_check})"
    )
    op.execute(
        f"ALTER TABLE type_registry_aliases ADD CONSTRAINT category_allowed CHECK ({_category_check})"
    )

    # Drop registry_categories
    op.drop_table("registry_categories")
"""Migration 0017 — Genesis meta-registry and capability groups."""
