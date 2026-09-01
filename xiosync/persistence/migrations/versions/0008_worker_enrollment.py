"""Worker enrollment & credential tables. Revision ID: 0008; Revises: 0007.

Creates the Phase 4 worker-enrollment tables (doc 07 §2):

* ``worker_enrollments`` — one row per compute-actor per org; owns the
  enrollment lifecycle (pending → approved → suspended → revoked), the
  worker's public key, and the hashed one-time enrollment token.
* ``worker_credentials`` — short-lived, capability-scoped credential records
  minted on issuance and revocable at any time (INV-WORKER-CRED-1).

Structural DDL mirrors the ORM metadata in
``persistence/models/workers`` (what autogenerate emits and the
INV-TEST-SCHEMA-2 diff gate compares against).  RLS org-isolation follows the
pattern set by revisions 0004–0007.

Reversibility (INV-MIG-3): downgrade drops both tables in FK-safe order.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None

# Standard tenant isolation GUC check (mirrors revisions 0004–0007).
_ORG_ISOLATION = (
    "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)

_WORKER_TABLES = ("worker_credentials", "worker_enrollments")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # worker_enrollments
    # ------------------------------------------------------------------
    op.create_table(
        "worker_enrollments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "enrollment_state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("pool_type", sa.Text(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("enrollment_token_hash", sa.Text(), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "approved_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        # PK
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_enrollments")),
        # organization FK
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_worker_enrollments_organization_id_organizations"),
        ),
        # Composite same-org FK: worker must be an actor in the same org.
        sa.ForeignKeyConstraint(
            ["organization_id", "worker_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_worker_enrollments_worker_same_org",
        ),
        # Composite same-org FK: approved_by must be an actor in the same org.
        sa.ForeignKeyConstraint(
            ["organization_id", "approved_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_worker_enrollments_approved_by_same_org",
        ),
        # Anchor for worker_credentials composite FK.
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name=op.f("uq_worker_enrollments_organization_id_id"),
        ),
        # One enrollment per worker per org.
        sa.UniqueConstraint(
            "organization_id",
            "worker_id",
            name="uq_worker_enrollments_org_worker",
        ),
        # Closed enrollment-state set.
        sa.CheckConstraint(
            "enrollment_state IN ('pending', 'approved', 'suspended', 'revoked')",
            name="enrollment_state_allowed",
        ),
        # Closed pool-type set.
        sa.CheckConstraint(
            "pool_type IN ('managed', 'volunteer')",
            name="pool_type_allowed",
        ),
    )

    # organization_id index (standard; mirrors all other tenant tables).
    op.create_index(
        op.f("ix_worker_enrollments_organization_id"),
        "worker_enrollments",
        ["organization_id"],
        unique=False,
    )
    # Hot path: list pending enrollments in an org.
    op.create_index(
        "ix_worker_enrollments_org_state",
        "worker_enrollments",
        ["organization_id", "enrollment_state"],
        unique=False,
    )

    # RLS policy (mirrors 0004/0005/0007 pattern).
    op.execute(
        "ALTER TABLE worker_enrollments ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"""
        CREATE POLICY worker_enrollments_org_isolation
            ON worker_enrollments
            USING ({_ORG_ISOLATION})
        """
    )

    # ------------------------------------------------------------------
    # worker_credentials
    # ------------------------------------------------------------------
    op.create_table(
        "worker_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "scoped_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "issued_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "expires_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        # PK
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_credentials")),
        # organization FK
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_worker_credentials_organization_id_organizations"),
        ),
        # Composite same-org FK: enrollment must be in the same org.
        sa.ForeignKeyConstraint(
            ["organization_id", "enrollment_id"],
            ["worker_enrollments.organization_id", "worker_enrollments.id"],
            name="fk_worker_credentials_enrollment_same_org",
        ),
    )

    # organization_id index.
    op.create_index(
        op.f("ix_worker_credentials_organization_id"),
        "worker_credentials",
        ["organization_id"],
        unique=False,
    )
    # List all credentials for a specific enrolled worker.
    op.create_index(
        "ix_worker_credentials_org_enrollment",
        "worker_credentials",
        ["organization_id", "enrollment_id"],
        unique=False,
    )
    # Efficient sweep: find active credentials expiring soon.
    op.create_index(
        "ix_worker_credentials_org_expires_active",
        "worker_credentials",
        ["organization_id", "expires_at"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # RLS policy.
    op.execute(
        "ALTER TABLE worker_credentials ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"""
        CREATE POLICY worker_credentials_org_isolation
            ON worker_credentials
            USING ({_ORG_ISOLATION})
        """
    )


def downgrade() -> None:
    # Drop in FK-safe reverse order.
    op.execute("DROP POLICY IF EXISTS worker_credentials_org_isolation ON worker_credentials")
    op.drop_index("ix_worker_credentials_org_expires_active", table_name="worker_credentials")
    op.drop_index("ix_worker_credentials_org_enrollment", table_name="worker_credentials")
    op.drop_index(
        op.f("ix_worker_credentials_organization_id"), table_name="worker_credentials"
    )
    op.drop_table("worker_credentials")

    op.execute(
        "DROP POLICY IF EXISTS worker_enrollments_org_isolation ON worker_enrollments"
    )
    op.drop_index("ix_worker_enrollments_org_state", table_name="worker_enrollments")
    op.drop_index(
        op.f("ix_worker_enrollments_organization_id"), table_name="worker_enrollments"
    )
    op.drop_table("worker_enrollments")
