"""Ontology graph & type-registry tables (docs 03 §§2.6-2.9, 8; 06 §§5, 8).

Four models live here:

* ``TypeRegistry`` / ``TypeRegistryAlias`` — the single authority for valid
  type values (doc 03 §8, H3 remediation). ``core.*`` namespace rows are
  platform-global and therefore the one deliberate ``organization_id``
  exemption on these tables (doc 06 §4/§8): the column is nullable, and
  tenant/plugin namespace rows carry their owning org.
* ``Edge`` — a typed, directed relationship between two actors (doc 03 §2.7).
* ``Memory`` — versioned knowledge owned by an actor (doc 03 §2.9). Never
  overwritten: a change writes a new row and points the prior row's
  ``superseded_by`` at the successor (INV-MEM-2, doc 06 §6).

Same-org referential integrity (INV-TABLE-1, doc 05 INV-TENANT-4): every FK to
a tenant-bearing row is a *composite* ``(organization_id, <id>)`` reference so a
cross-org reference cannot be committed. ``edges`` and ``memory`` therefore
reference ``actors(organization_id, id)``; ``memory.superseded_by`` is a
same-org self reference anchored on this table's ``(organization_id, id)``
unique constraint.

Registry-validated columns (``category`` aside) get no value CHECK: valid type
values are Type-Registry data (doc 03 §8), and hardcoding them would recreate
H3. Only the closed sets doc 03 fixes in the schema (graph classes, edge/
memory states, memory kind/visibility, registry category) get CHECKs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_timestamptz = TIMESTAMP(timezone=True)

# Doc 03 §8 / doc 06 §8: categories are now validated via FK to
# registry_categories table (migration 0017 — Genesis meta-registry).
# The former hardcoded CHECK was:
#   category IN ('actor_type', 'actor_subtype', 'capability_type',
#                'operation_type', 'edge_type', 'event_type', 'lifecycle_state')
# New categories can be registered at runtime without DDL.
# _CATEGORY_CHECK is retained only for reference in tests/downgrade scripts.
_LEGACY_CATEGORY_CHECK = (
    "category IN ("
    "'actor_type', 'actor_subtype', 'capability_type', 'operation_type', "
    "'edge_type', 'event_type', 'lifecycle_state')"
)


class TypeRegistry(Base):
    """Namespaced, versioned authority for valid type values (doc 03 §8)."""

    __tablename__ = "type_registry"
    __table_args__ = (
        # Anchor for the aliases composite same-org FK and for uniqueness of a
        # concrete (namespace, category, value, version) definition.
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint(
            "namespace",
            "category",
            "value",
            "version",
            name="uq_type_registry_namespace_category_value_version",
        ),
        # Genesis meta-registry: category validated by FK to registry_categories
        # (replaces former CHECK constraint, migration 0017).
        ForeignKeyConstraint(
            ["category"],
            ["registry_categories.name"],
            name="fk_type_registry_category",
        ),
        CheckConstraint("state IN ('active', 'deprecated')", name="state_allowed"),
        Index("ix_type_registry_lookup", "namespace", "category", "value"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    # NULL for the platform-global ``core.*`` namespace (doc 06 §4/§8 exemption);
    # non-null and immutable for tenant/plugin namespaces.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    namespace: Mapped[str] = mapped_column(Text, nullable=False)  # 'core' or tenant/plugin ns
    category: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    state: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
    updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)


class TypeRegistryAlias(Base):
    """Deprecation / migration alias mapping an old value to a canonical one."""

    __tablename__ = "type_registry_aliases"
    __table_args__ = (
        # An alias for a given (ns, category, value) is unique; the namespace
        # matches its target definition's namespace (doc 06 §5).
        UniqueConstraint(
            "namespace",
            "category",
            "alias_value",
            name="uq_type_registry_aliases_namespace_category_alias_value",
        ),
        # Same-org (incl. NULL/core) target: an alias resolves to a registry row
        # in the same namespace scope.
        ForeignKeyConstraint(
            ["organization_id", "target_id"],
            ["type_registry.organization_id", "type_registry.id"],
            name="fk_type_registry_aliases_target_same_org",
        ),
        # Genesis meta-registry: category validated by FK to registry_categories
        # (replaces former CHECK constraint, migration 0017).
        ForeignKeyConstraint(
            ["category"],
            ["registry_categories.name"],
            name="fk_type_registry_aliases_category",
        ),
        Index("ix_type_registry_aliases_lookup", "namespace", "category", "alias_value"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )  # NULL for core.* (matches ns)
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    alias_value: Mapped[str] = mapped_column(Text, nullable=False)  # deprecated / migrated-from
    canonical_value: Mapped[str] = mapped_column(Text, nullable=False)  # resolves-to value
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM


class Edge(Base):
    """A typed, directed relationship between two actors (doc 03 §2.7)."""

    __tablename__ = "edges"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        # INV-EDGE-1: source and target are in the edge's organization.
        ForeignKeyConstraint(
            ["organization_id", "source_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_edges_source_same_org",
        ),
        ForeignKeyConstraint(
            ["organization_id", "target_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_edges_target_same_org",
        ),
        # Graph class fixes the acyclicity rule (doc 03 §3); the value itself is
        # a closed schema set, unlike edge_type which is registry-validated.
        CheckConstraint(
            "graph_class IN ('hierarchy', 'workflow', 'relationship', 'dependency')",
            name="graph_class_allowed",
        ),
        CheckConstraint("state IN ('active', 'inactive')", name="state_allowed"),
        # Traversal hot paths lead with organization_id (doc 06 §7).
        Index("ix_edges_org_source", "organization_id", "source_id"),
        Index("ix_edges_org_target", "organization_id", "target_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)  # registry-validated
    graph_class: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM


class Memory(Base):
    """Versioned knowledge owned by an actor (doc 03 §2.9)."""

    __tablename__ = "memory"
    __table_args__ = (
        # Anchor for the same-org self reference below.
        UniqueConstraint("organization_id", "id"),
        # INV-MEM-1: memory never crosses organizations; the owner is same-org.
        ForeignKeyConstraint(
            ["organization_id", "owner_actor_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_memory_owner_actor_same_org",
        ),
        # INV-MEM-2: supersession points at a newer version in the same org.
        ForeignKeyConstraint(
            ["organization_id", "superseded_by"],
            ["memory.organization_id", "memory.id"],
            name="fk_memory_superseded_by_same_org",
        ),
        CheckConstraint(
            "kind IN ('observation', 'intention', 'outcome', 'fact')",
            name="kind_allowed",
        ),
        CheckConstraint("visibility IN ('private', 'org_shared')", name="visibility_allowed"),
        Index("ix_memory_org_owner", "organization_id", "owner_actor_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    owner_actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    embedding_ref: Mapped[str | None] = mapped_column(Text)  # vector-store pointer
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # source op/event, confidence
    # Gap D-3: artifact references linking memory to generated artifacts.
    artifact_refs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
