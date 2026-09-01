"""Wave 2A — Schema foundation for capabilities blueprint, event indexing, and artifacts.

Revision ID: 0011
Revises: 0010

Gap X-2: Expand ``capabilities`` table with blueprint columns (input_schema,
output_schema, execution_mode, timeout_ms, retry_policy, version, state).
Note: ``organization_id`` stays NOT NULL for now; global capabilities will be
addressed in Wave 3 with a dual-lookup pattern to preserve FK integrity.

Gap R-4: Promote event routing metadata from ``payload`` JSONB into first-class
indexed columns (severity, correlation_id, operation_id, entity_type, entity_id).

Gap D-1: Create ``artifacts`` table for provider-agnostic artifact references.
The platform stores metadata only, never proxies raw bytes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None

# Standard tenant isolation predicate.
_ORG_ISOLATION = (
    "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # X-2: capabilities table — blueprint columns
    # ------------------------------------------------------------------
    op.add_column(
        "capabilities",
        sa.Column(
            "input_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "capabilities",
        sa.Column(
            "output_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "capabilities",
        sa.Column(
            "execution_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'sync'"),
        ),
    )
    op.add_column(
        "capabilities",
        sa.Column(
            "timeout_ms",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column(
        "capabilities",
        sa.Column(
            "retry_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "capabilities",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "capabilities",
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
    )
    op.create_check_constraint(
        "execution_mode_allowed",
        "capabilities",
        "execution_mode IN ('sync', 'async', 'streaming')",
    )
    op.create_check_constraint(
        "state_allowed",
        "capabilities",
        "state IN ('draft', 'active', 'deprecated')",
    )
    op.create_check_constraint(
        "timeout_ms_positive",
        "capabilities",
        "timeout_ms IS NULL OR timeout_ms > 0",
    )

    # ------------------------------------------------------------------
    # R-4: events table — first-class indexable columns
    # ------------------------------------------------------------------
    op.add_column(
        "events",
        sa.Column(
            "severity",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'info'"),
        ),
    )
    op.add_column(
        "events",
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("entity_type", sa.Text(), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint(
        "severity_allowed",
        "events",
        "severity IN ('debug', 'info', 'warn', 'error', 'critical')",
    )
    op.create_index(
        "ix_events_org_type_severity",
        "events",
        ["organization_id", "event_type", "severity"],
    )
    op.create_index(
        "ix_events_org_correlation",
        "events",
        ["organization_id", "correlation_id"],
    )
    op.create_index(
        "ix_events_org_entity",
        "events",
        ["organization_id", "entity_type", "entity_id"],
    )

    # ------------------------------------------------------------------
    # D-1: artifacts table — provider-agnostic metadata references
    # ------------------------------------------------------------------
    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("provider_type", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_artifacts_created_by_same_org",
        ),
        sa.CheckConstraint(
            "provider_type IN ('r2', 's3', 'gcs', 'azure_blob', 'local', 'inline', 'custom')",
            name="ck_artifacts_provider_type_allowed",
        ),
    )
    op.create_index(
        "ix_artifacts_organization_id",
        "artifacts",
        ["organization_id"],
    )
    op.create_index(
        "ix_artifacts_org_provider",
        "artifacts",
        ["organization_id", "provider_type"],
    )

    # RLS on artifacts
    op.execute("ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE artifacts FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY org_isolation ON artifacts USING ({_ORG_ISOLATION})"
    )


def downgrade() -> None:
    # D-1: drop artifacts
    op.execute("DROP POLICY IF EXISTS org_isolation ON artifacts")
    op.execute("ALTER TABLE artifacts NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE artifacts DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_artifacts_org_provider", table_name="artifacts")
    op.drop_index("ix_artifacts_organization_id", table_name="artifacts")
    op.drop_table("artifacts")

    # R-4: drop events columns & indexes
    op.drop_index("ix_events_org_entity", table_name="events")
    op.drop_index("ix_events_org_correlation", table_name="events")
    op.drop_index("ix_events_org_type_severity", table_name="events")
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_severity_allowed")
    op.drop_column("events", "entity_id")
    op.drop_column("events", "entity_type")
    op.drop_column("events", "operation_id")
    op.drop_column("events", "correlation_id")
    op.drop_column("events", "severity")

    # X-2: drop capabilities blueprint columns
    op.execute("ALTER TABLE capabilities DROP CONSTRAINT ck_capabilities_timeout_ms_positive")
    op.execute("ALTER TABLE capabilities DROP CONSTRAINT ck_capabilities_state_allowed")
    op.execute("ALTER TABLE capabilities DROP CONSTRAINT ck_capabilities_execution_mode_allowed")
    op.drop_column("capabilities", "state")
    op.drop_column("capabilities", "version")
    op.drop_column("capabilities", "retry_policy")
    op.drop_column("capabilities", "timeout_ms")
    op.drop_column("capabilities", "execution_mode")
    op.drop_column("capabilities", "output_schema")
    op.drop_column("capabilities", "input_schema")
