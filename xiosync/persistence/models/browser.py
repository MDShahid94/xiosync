"""Browser orchestration models for XIOBR decoupling."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_ts = TIMESTAMP(timezone=True)


class BrowserPool(Base):
    """Configuration for a pool of browser instances."""

    __tablename__ = "browser_pools"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_browser_pools_org_id"),
        CheckConstraint(
            "state IN ('active', 'suspended', 'archived')",
            name="ck_browser_pools_state",
        ),
        UniqueConstraint("organization_id", "name", name="uq_browser_pools_org_name"),
        UniqueConstraint("organization_id", "id", name="uq_browser_pools_org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    engine_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="chromium")
    max_instances: Mapped[int] = mapped_column(Integer, nullable=False, server_default="10")
    stealth_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime | None] = mapped_column(_ts)

    sessions: Mapped[list[BrowserSession]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )


class ComputeRuntime(Base):
    """Abstracted runtime provider (Colab, AWS, bare metal, etc.)."""

    __tablename__ = "compute_runtimes"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_compute_runtimes_org_id"),
        CheckConstraint(
            "state IN ('active', 'offline', 'deprecated')",
            name="ck_compute_runtimes_state",
        ),
        UniqueConstraint("organization_id", "name", name="uq_compute_runtimes_org_name"),
        UniqueConstraint("organization_id", "id", name="uq_compute_runtimes_org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime | None] = mapped_column(_ts)

    nodes: Mapped[list[RuntimeNode]] = relationship(
        back_populates="runtime", cascade="all, delete-orphan"
    )


class RuntimeNode(Base):
    """An individual compute node registered in a runtime."""

    __tablename__ = "runtime_nodes"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_runtime_nodes_org_id"),
        CheckConstraint(
            "state IN ('provisioning', 'ready', 'busy', 'offline', 'terminated')",
            name="ck_runtime_nodes_state",
        ),
        Index("ix_runtime_nodes_runtime", "runtime_id"),
        UniqueConstraint("organization_id", "id", name="uq_runtime_nodes_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "runtime_id"],
            ["compute_runtimes.organization_id", "compute_runtimes.id"],
            name="fk_runtime_nodes_runtime_same_org",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True,
    )
    runtime_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
    )
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(Text)
    node_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="provisioning")
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime | None] = mapped_column(_ts)

    runtime: Mapped[ComputeRuntime] = relationship(back_populates="nodes")
    sessions: Mapped[list[BrowserSession]] = relationship(back_populates="node", overlaps="sessions")


class BrowserSession(Base):
    """An active or completed browser session."""

    __tablename__ = "browser_sessions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_browser_sessions_org_id"),
        CheckConstraint(
            "state IN ('initializing', 'active', 'suspended', 'terminated', 'failed')",
            name="ck_browser_sessions_state",
        ),
        Index("ix_browser_sessions_pool", "pool_id"),
        ForeignKeyConstraint(
            ["organization_id", "pool_id"],
            ["browser_pools.organization_id", "browser_pools.id"],
            name="fk_browser_sessions_pool_same_org",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "node_id"],
            ["runtime_nodes.organization_id", "runtime_nodes.id"],
            name="fk_browser_sessions_node_same_org",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True,
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    session_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="initializing")
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime | None] = mapped_column(_ts)

    pool: Mapped[BrowserPool] = relationship(back_populates="sessions", overlaps="sessions")
    node: Mapped[RuntimeNode | None] = relationship(back_populates="sessions", overlaps="pool,sessions")


class MeshNetwork(Base):
    """Network mesh configurations (Tailscale, WireGuard, etc.)."""

    __tablename__ = "mesh_networks"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_mesh_networks_org_id"),
        CheckConstraint(
            "state IN ('active', 'configuring', 'error', 'disabled')",
            name="ck_mesh_networks_state",
        ),
        UniqueConstraint("organization_id", "name", name="uq_mesh_networks_org_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    network_type: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="configuring")
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime | None] = mapped_column(_ts)


class MeshNode(Base):
    __tablename__ = "mesh_nodes"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_mesh_nodes_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "network_id"],
            ["mesh_networks.organization_id", "mesh_networks.id"],
            name="fk_mesh_nodes_network_same_org",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True,
    )
    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
    )
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_ts, nullable=False, server_default=text("now()"))
