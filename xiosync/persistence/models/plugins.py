"""Sandboxed-plugin ORM models (doc 07 §5; DECISIONS.md D-008; Phase 5).

Four tables model the plugin subsystem's control-plane state. Plugins execute
out-of-process (never in an API process); these tables only *describe and
govern* them — they hold no plugin code.

* ``Plugin`` — a registered plugin *manifest*: identity (``name``/``version``),
  the launch ``entrypoint``, the ``required_capability`` Grant that gates it, its
  resource quota (``cpu_millis``/``memory_mb``/``timeout_seconds``) and
  ``filesystem_jail`` (INV-PLUGIN-1), and a ``manifest_hash`` for integrity.
* ``PluginRpcMethod`` — one row per typed host↔plugin RPC method with JSON-Schema
  ``input_schema``/``output_schema`` (INV-PLUGIN-2).
* ``PluginInstallation`` — an approval-gated install of a plugin into an org. It
  lands in ``pending_approval`` and only an explicit, authorized approval moves
  it forward (INV-PLUGIN-3); ``grant_id`` links the capability Grant minted on
  approval.
* ``PluginNetworkAllowRule`` — one explicitly-allowed network destination per
  installation. The **absence** of rows is a fully-closed (deny-all) policy;
  there is no allow-all row (INV-PLUGIN-4). The allowlist is scoped to the
  installation so each org's install has its own network stance.

Same-org referential integrity (INV-TABLE-1 / INV-TENANT-4, doc 05): every FK to
a tenant-bearing row is a composite ``(organization_id, <id>)`` reference so a
cross-org link cannot be committed. Closed state/protocol sets get ``CHECK``
constraints mirroring the ``domain/plugins`` frozensets; the registry-free,
schema-fixed enums live here, while JSON-Schema payloads stay opaque ``jsonb``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
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


class Plugin(Base):
    """A registered plugin manifest (doc 07 §5, INV-PLUGIN-1/2)."""

    __tablename__ = "plugins"
    __table_args__ = (
        # Anchor for the rpc-method / installation composite same-org FKs.
        UniqueConstraint("organization_id", "id"),
        # A plugin's (name, version) is unique within its org; a new manifest is
        # a new version row, never an in-place edit (mirrors workflows).
        UniqueConstraint(
            "organization_id",
            "name",
            "version",
            name="uq_plugins_organization_id_name_version",
        ),
        # INV-PLUGIN-1: the required capability Grant is an actual capability in
        # the same org (composite same-org FK — INV-TABLE-1).
        ForeignKeyConstraint(
            ["organization_id", "required_capability_id"],
            ["capabilities.organization_id", "capabilities.id"],
            name="fk_plugins_required_capability_same_org",
        ),
        # Closed manifest lifecycle set (domain/plugins.PLUGIN_STATES).
        CheckConstraint(
            "state IN ('registered', 'deprecated')",
            name="state_allowed",
        ),
        # INV-PLUGIN-1: quotas are strictly positive — an unbounded sandbox is
        # exactly the ambient-access failure the sandbox prevents.
        CheckConstraint("cpu_millis > 0", name="cpu_millis_positive"),
        CheckConstraint("memory_mb > 0", name="memory_mb_positive"),
        CheckConstraint("timeout_seconds > 0", name="timeout_seconds_positive"),
        # Hot path: list plugins in an org by lifecycle state.
        Index("ix_plugins_org_state", "organization_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )  # IMM
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    entrypoint: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # opaque image ref / module the sandbox host launches
    required_capability_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )  # IMM — INV-PLUGIN-1
    cpu_millis: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    filesystem_jail: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # jail root (INV-PLUGIN-1)
    manifest_hash: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # integrity hash of the canonicalized manifest
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'registered'")
    )
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
    updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)


class PluginRpcMethod(Base):
    """A typed host↔plugin RPC method contract (INV-PLUGIN-2)."""

    __tablename__ = "plugin_rpc_methods"
    __table_args__ = (
        # INV-TABLE-1: the method belongs to a plugin in the same org.
        ForeignKeyConstraint(
            ["organization_id", "plugin_id"],
            ["plugins.organization_id", "plugins.id"],
            name="fk_plugin_rpc_methods_plugin_same_org",
        ),
        # One method name per plugin (INV-PLUGIN-2: a narrow, well-defined surface).
        UniqueConstraint(
            "organization_id",
            "plugin_id",
            "method_name",
            name="uq_plugin_rpc_methods_org_plugin_method",
        ),
        # List a plugin's RPC surface.
        Index(
            "ix_plugin_rpc_methods_org_plugin",
            "organization_id",
            "plugin_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )  # IMM
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    plugin_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # IMM
    method_name: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )  # JSON Schema for call arguments
    output_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )  # JSON Schema for the reply payload
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM


class PluginInstallation(Base):
    """An approval-gated installation of a plugin into an org (INV-PLUGIN-3)."""

    __tablename__ = "plugin_installations"
    __table_args__ = (
        # Anchor for the network-allow-rule composite same-org FK.
        UniqueConstraint("organization_id", "id"),
        # One installation per plugin per org.
        UniqueConstraint(
            "organization_id",
            "plugin_id",
            name="uq_plugin_installations_org_plugin",
        ),
        # INV-TABLE-1: plugin, requester, approver and grant are all same-org.
        ForeignKeyConstraint(
            ["organization_id", "plugin_id"],
            ["plugins.organization_id", "plugins.id"],
            name="fk_plugin_installations_plugin_same_org",
        ),
        ForeignKeyConstraint(
            ["organization_id", "requested_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_plugin_installations_requested_by_same_org",
        ),
        ForeignKeyConstraint(
            ["organization_id", "approved_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_plugin_installations_approved_by_same_org",
        ),
        # grant_id is nullable: it is minted on approval (INV-PLUGIN-1/3).
        ForeignKeyConstraint(
            ["organization_id", "grant_id"],
            ["grants.organization_id", "grants.id"],
            name="fk_plugin_installations_grant_same_org",
        ),
        # Closed installation lifecycle set (domain/plugins.INSTALLATION_STATES).
        CheckConstraint(
            "state IN ('pending_approval', 'approved', 'active', 'suspended', 'revoked')",
            name="state_allowed",
        ),
        # Hot path: list installs in an org by lifecycle state (e.g. pending queue).
        Index(
            "ix_plugin_installations_org_state",
            "organization_id",
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )  # IMM
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    plugin_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # IMM
    # INV-PLUGIN-3: lands 'pending_approval'; advancing is an explicit, authorized act.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending_approval'")
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # IMM
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # set on approval
    # The capability Grant minted when the install is approved (INV-PLUGIN-1);
    # null while pending.
    grant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    requested_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
    approved_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
    updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)


class PluginNetworkAllowRule(Base):
    """One explicitly-allowed network destination for an installation (INV-PLUGIN-4).

    The absence of rows for an installation is a fully-closed (deny-all) policy.
    There is no allow-all row: allow-all host sentinels are rejected in
    ``domain/plugins.NetworkAllowRule`` before a row is ever attempted.
    """

    __tablename__ = "plugin_network_allow_rules"
    __table_args__ = (
        # INV-TABLE-1: the rule belongs to an installation in the same org.
        ForeignKeyConstraint(
            ["organization_id", "installation_id"],
            ["plugin_installations.organization_id", "plugin_installations.id"],
            name="fk_plugin_network_allow_rules_installation_same_org",
        ),
        # A destination triple is unique per installation.
        UniqueConstraint(
            "organization_id",
            "installation_id",
            "host",
            "port",
            "protocol",
            name="uq_plugin_network_allow_rules_installation_destination",
        ),
        # Closed protocol set (domain/plugins.ALLOWED_PROTOCOLS).
        CheckConstraint(
            "protocol IN ('tcp', 'udp', 'http', 'https', 'tls')",
            name="protocol_allowed",
        ),
        CheckConstraint("port >= 1 AND port <= 65535", name="port_range"),
        # Load an installation's allowlist for enforcement.
        Index(
            "ix_plugin_network_allow_rules_org_installation",
            "organization_id",
            "installation_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )  # IMM
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )  # IMM
    host: Mapped[str] = mapped_column(Text, nullable=False)  # concrete host/IP; no wildcards
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
