"""Ontology graph & type registry tables. Revision ID: 0005; Revises: 0004.

Creates the Phase 2 ontology spine: ``type_registry`` and
``type_registry_aliases`` (doc 03 §8 / doc 06 §8), ``operations`` (doc 03 §2.6),
``edges`` (doc 03 §2.7) and ``memory`` (doc 03 §2.9).

Structural DDL mirrors the ORM metadata (what autogenerate emits). RLS
org-isolation is applied by hand — autogenerate does not manage it — following
the precedent set by revision 0004. The type-registry tables use a relaxed
predicate because their ``core.*`` rows are platform-global (nullable
``organization_id``, doc 06 §4/§8) and must be readable from every org.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None

_CATEGORY_CHECK = (
    "category IN ("
    "'actor_type', 'actor_subtype', 'capability_type', 'operation_type', "
    "'edge_type', 'event_type', 'lifecycle_state')"
)

# Standard tenant isolation: row's org must equal the request's current org.
_ORG_ISOLATION = "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
# Registry isolation: platform-global core.* rows (NULL org) are visible to all,
# plus the current org's own namespace rows.
_REGISTRY_ISOLATION = (
    "organization_id IS NULL OR "
    "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "type_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
        ),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "definition", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint(
            "namespace",
            "category",
            "value",
            "version",
            name="uq_type_registry_namespace_category_value_version",
        ),
        sa.CheckConstraint(_CATEGORY_CHECK, name="ck_type_registry_category_allowed"),
        sa.CheckConstraint(
            "state IN ('active', 'deprecated')", name="ck_type_registry_state_allowed"
        ),
    )
    op.create_index("ix_type_registry_organization_id", "type_registry", ["organization_id"])
    op.create_index("ix_type_registry_lookup", "type_registry", ["namespace", "category", "value"])

    op.create_table(
        "type_registry_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
        ),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("alias_value", sa.Text(), nullable=False),
        sa.Column("canonical_value", sa.Text(), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "namespace",
            "category",
            "alias_value",
            name="uq_type_registry_aliases_namespace_category_alias_value",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "target_id"],
            ["type_registry.organization_id", "type_registry.id"],
            name="fk_type_registry_aliases_target_same_org",
        ),
        sa.CheckConstraint(_CATEGORY_CHECK, name="ck_type_registry_aliases_category_allowed"),
    )
    op.create_index(
        "ix_type_registry_aliases_organization_id",
        "type_registry_aliases",
        ["organization_id"],
    )
    op.create_index(
        "ix_type_registry_aliases_lookup",
        "type_registry_aliases",
        ["namespace", "category", "alias_value"],
    )

    op.create_table(
        "operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("from_state", sa.Text()),
        sa.Column("to_state", sa.Text()),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("initiated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "collaborators",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("scope", sa.Text()),
        sa.Column("depth_level", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("parent_operation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("artifacts", postgresql.JSONB()),
        sa.Column("rationale", sa.Text()),
        sa.Column("outcome", sa.Text()),
        sa.Column(
            "started_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_operations_actor_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "initiated_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_operations_initiated_by_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "parent_operation_id"],
            ["operations.organization_id", "operations.id"],
            name="fk_operations_parent_same_org",
        ),
        sa.CheckConstraint(
            "trigger IN ('user_command', 'schedule', 'auto', 'error', 'system')",
            name="ck_operations_trigger_allowed",
        ),
        sa.CheckConstraint(
            "scope IN ('actor', 'component', 'organization')",
            name="ck_operations_scope_allowed",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'partial', 'failed', 'pending')",
            name="ck_operations_outcome_allowed",
        ),
    )
    op.create_index("ix_operations_organization_id", "operations", ["organization_id"])
    op.create_index(
        "ix_operations_org_actor_started",
        "operations",
        ["organization_id", "actor_id", "started_at"],
    )

    op.create_table(
        "edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("edge_type", sa.Text(), nullable=False),
        sa.Column("graph_class", sa.Text(), nullable=False),
        sa.Column("weight", sa.Float()),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_edges_source_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "target_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_edges_target_same_org",
        ),
        sa.CheckConstraint(
            "graph_class IN ('hierarchy', 'workflow', 'relationship', 'dependency')",
            name="ck_edges_graph_class_allowed",
        ),
        sa.CheckConstraint("state IN ('active', 'inactive')", name="ck_edges_state_allowed"),
    )
    op.create_index("ix_edges_organization_id", "edges", ["organization_id"])
    op.create_index("ix_edges_org_source", "edges", ["organization_id", "source_id"])
    op.create_index("ix_edges_org_target", "edges", ["organization_id", "target_id"])

    op.create_table(
        "memory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("owner_actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("embedding_ref", sa.Text()),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("provenance", postgresql.JSONB()),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_actor_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_memory_owner_actor_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "superseded_by"],
            ["memory.organization_id", "memory.id"],
            name="fk_memory_superseded_by_same_org",
        ),
        sa.CheckConstraint(
            "kind IN ('observation', 'intention', 'outcome', 'fact')",
            name="ck_memory_kind_allowed",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'org_shared')", name="ck_memory_visibility_allowed"
        ),
    )
    op.create_index("ix_memory_organization_id", "memory", ["organization_id"])
    op.create_index("ix_memory_org_owner", "memory", ["organization_id", "owner_actor_id"])

    # Row-Level Security (hand-applied; not autogenerated). Standard tenant
    # tables use strict org isolation; the type-registry tables allow global
    # core.* rows through in addition to the current org's own rows.
    for table in ("operations", "edges", "memory"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_{table}_org_isolation ON {table} "
            f"USING ({_ORG_ISOLATION}) WITH CHECK ({_ORG_ISOLATION})"
        )
    for table in ("type_registry", "type_registry_aliases"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_{table}_org_isolation ON {table} "
            f"USING ({_REGISTRY_ISOLATION}) WITH CHECK ({_REGISTRY_ISOLATION})"
        )


def downgrade() -> None:
    op.drop_table("memory")
    op.drop_table("edges")
    op.drop_table("operations")
    op.drop_table("type_registry_aliases")
    op.drop_table("type_registry")
