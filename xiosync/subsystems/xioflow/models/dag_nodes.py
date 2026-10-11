"""XIOFLOW DAG Nodes and Edges models — relational graph execution."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Float, ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_timestamptz = TIMESTAMP(timezone=True)


class DAGNode(Base):
    """Relational node in a DAG workflow template."""

    __tablename__ = "xioflow_dag_nodes"
    __table_args__ = (
        UniqueConstraint("template_id", "node_key", name="uq_dag_node_key_per_template"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_key: Mapped[str] = mapped_column(Text, nullable=False)
    node_type: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    position_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[Any] = mapped_column(_timestamptz, nullable=False, server_default=text("now()"))


class DAGEdge(Base):
    """Relational directed edge between nodes in a DAG workflow template."""

    __tablename__ = "xioflow_dag_edges"
    __table_args__ = (
        UniqueConstraint("template_id", "source_node_id", "target_node_id", name="uq_dag_edge_per_template"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("xioflow_dag_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("xioflow_dag_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    condition: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    edge_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[Any] = mapped_column(_timestamptz, nullable=False, server_default=text("now()"))
