"""Authorization spine tables. Revision ID: 0004; Revises: 0003."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capabilities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "name"),
    )
    op.create_index("ix_capabilities_organization_id", "capabilities", ["organization_id"])
    op.create_table(
        "grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "constraints", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True)),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_grants_actor_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "capability_id"],
            ["capabilities.organization_id", "capabilities.id"],
            name="fk_grants_capability_same_org",
        ),
        sa.CheckConstraint("state IN ('active', 'revoked')", name="ck_grants_state_allowed"),
        # Composite unique anchor for the 0009 plugin_installations composite FK
        # REFERENCES grants(organization_id, id).  PostgreSQL requires the
        # referenced column set to be covered by a UNIQUE or PRIMARY KEY
        # constraint; the single-column PK on id alone does not satisfy it.
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_grants_organization_id_id",
        ),
    )
    op.create_index("ix_grants_organization_id", "grants", ["organization_id"])
    op.create_index(
        "ix_grants_actor_capability_active",
        "grants",
        ["organization_id", "actor_id", "capability_id"],
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_events_organization_id", "events", ["organization_id"])
    op.create_index(
        "ix_events_org_type_created", "events", ["organization_id", "event_type", "created_at"]
    )
    for table in ("capabilities", "grants", "events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        predicate = "organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
        op.execute(
            f"CREATE POLICY rls_{table}_org_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    op.execute(
        "CREATE FUNCTION reject_event_mutation() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'events are append-only'; END $$"
    )
    op.execute(
        "CREATE TRIGGER trg_events_append_only BEFORE UPDATE OR DELETE ON events "
        "FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_events_append_only ON events")
    op.execute("DROP FUNCTION reject_event_mutation()")
    op.drop_table("events")
    op.drop_table("grants")
    op.drop_table("capabilities")
