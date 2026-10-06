"""SQLAlchemy model: AccountIpBinding — per-Google-account sticky PPPoE slot."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id


class AccountIpBinding(Base):
    """Binds a Google account email to a specific PPPoE slot on a host.

    Ensures that "etathyaghar@gmail.com" always exits via the same residential
    IP across sessions. Google detecting an IP change for the same account
    triggers verification prompts / risk signals.

    Uniqueness: one binding per (organization_id, google_account) — an account
    always has exactly one bound slot within an org.
    """
    __tablename__ = "xiogrid_account_ip_bindings"
    __table_args__ = (
        UniqueConstraint("organization_id", "google_account",
                         name="uq_account_ip_binding_per_org"),
        Index("ix_account_ip_bindings_lookup", "organization_id", "google_account"),
        Index("ix_account_ip_bindings_slot",   "host_id", "ppp_slot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id,
        name="id",
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE",
                   name="fk_account_ip_bindings_org"),
        nullable=False,
    )
    # Full email address: "etathyaghar@gmail.com"
    google_account: Mapped[str] = mapped_column(Text, nullable=False)

    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("xiogrid_pppoe_hosts.id", ondelete="CASCADE",
                   name="fk_account_ip_bindings_host"),
        nullable=False,
    )
    ppp_slot: Mapped[int] = mapped_column(Integer, nullable=False)

    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default="now()",
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    total_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Cache last known public IP — informational only, slot is the source of truth
    last_public_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
