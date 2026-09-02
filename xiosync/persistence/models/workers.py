"""WorkerEnrollment & WorkerCredential ORM models (doc 07 §2; Phase 4).

Two tables model the worker-enrollment lifecycle and per-worker, short-lived
capability-scoped credentials:

* ``WorkerEnrollment`` — one row per worker per org; owns the enrollment
  lifecycle state (pending → approved → suspended → revoked) and the public
  key used for mutual authentication.  Composite same-org FK to ``actors``
  ensures the referenced worker is always in the same tenant boundary
  (INV-TENANT-4).  ``approved_by`` is a second composite same-org FK to
  ``actors`` (nullable — pending enrollments have no approver yet).

* ``WorkerCredential`` — a short-lived, capability-scoped token record minted
  on each successful credential issuance (INV-WORKER-CRED-1).  Revocation is
  a write to ``revoked_at``; the credential sweep index makes expiry-driven
  garbage collection efficient.

Same-org referential integrity (INV-TABLE-1): every FK to a tenant-bearing
row is a composite ``(organization_id, <id>)`` reference.
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xiosync.persistence.models.base import Base
from xiosync.platform.ids import new_id

_timestamptz = TIMESTAMP(timezone=True)


class WorkerEnrollment(Base):
    """One enrollment record per worker per org (doc 07 §2)."""

    __tablename__ = "worker_enrollments"
    __table_args__ = (
        # Anchor for the worker_credentials composite same-org FK.
        UniqueConstraint("organization_id", "id"),
        # INV-TENANT-4: worker must be an actor in the same org.
        ForeignKeyConstraint(
            ["organization_id", "worker_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_worker_enrollments_worker_same_org",
        ),
        # approved_by is nullable — set only after approval.
        ForeignKeyConstraint(
            ["organization_id", "approved_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_worker_enrollments_approved_by_same_org",
        ),
        CheckConstraint(
            "enrollment_state IN ('pending', 'approved', 'suspended', 'revoked')",
            name="enrollment_state_allowed",
        ),
        CheckConstraint(
            "pool_type IN ('managed', 'volunteer')",
            name="pool_type_allowed",
        ),
        # Hot path: list all pending enrollments within an org.
        Index(
            "ix_worker_enrollments_org_state",
            "organization_id",
            "enrollment_state",
        ),
        # One enrollment per worker per org (INV-WORKER-ENROLL-1).
        UniqueConstraint(
            "organization_id",
            "worker_id",
            name="uq_worker_enrollments_org_worker",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )  # IMM
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )  # IMM — actor_type='compute' enforced at service layer
    enrollment_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    pool_type: Mapped[str] = mapped_column(Text, nullable=False)
    public_key: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # PEM or base64 ed25519 — opaque to schema
    enrollment_token_hash: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # hash of the one-time enrollment token; never stored plaintext
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )  # set when approved
    approved_at: Mapped[datetime | None] = mapped_column(_timestamptz)
    # Gap W-4: worker fleet versioning — track software version and digest.
    software_version: Mapped[str | None] = mapped_column(Text)
    software_hash: Mapped[str | None] = mapped_column(Text)
    # Gap W-4 + partial W-1: capability manifest — list of capability names
    # the worker can handle, used for future capability-based dispatch.
    capability_manifest: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
    updated_at: Mapped[datetime | None] = mapped_column(_timestamptz)


class WorkerCredential(Base):
    """A short-lived, capability-scoped worker credential (INV-WORKER-CRED-1)."""

    __tablename__ = "worker_credentials"
    __table_args__ = (
        # INV-TENANT-4: enrollment must be in the same org.
        ForeignKeyConstraint(
            ["organization_id", "enrollment_id"],
            ["worker_enrollments.organization_id", "worker_enrollments.id"],
            name="fk_worker_credentials_enrollment_same_org",
        ),
        # List all credentials for a specific enrolled worker.
        Index(
            "ix_worker_credentials_org_enrollment",
            "organization_id",
            "enrollment_id",
        ),
        # Efficient sweep: find credentials expiring soon that are still active.
        Index(
            "ix_worker_credentials_org_expires_active",
            "organization_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )  # IMM
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )  # IMM
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )  # IMM
    scoped_capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )  # list of capability UUIDs (as strings) granted to this credential
    issued_at: Mapped[datetime] = mapped_column(
        _timestamptz, nullable=False, server_default=text("now()")
    )  # IMM
    expires_at: Mapped[datetime] = mapped_column(_timestamptz, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        _timestamptz
    )  # null = active; set on explicit revocation
