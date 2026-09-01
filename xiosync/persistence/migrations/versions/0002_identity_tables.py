"""identity tables — organizations, actors, auth_identities, memberships, sessions

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-15

The Phase 1 first slice (doc 10 Phase 1; docs 03 §2, 05 §3, 06 §4-5). Table
DDL was produced by autogenerate from ``persistence/models/identity.py`` so
the drift gate (INV-TEST-SCHEMA-2) compares equal by construction. On top of
the tables, this revision adds what the ORM metadata deliberately does not
carry (autogenerate does not manage it):

- **Row-Level Security** (doc 05 §3.2, layer 2 of tenant isolation): every
  tenant-bearing table is ``ENABLE``d + ``FORCE``d with a single ``FOR ALL``
  policy keyed on the per-transaction ``app.current_org`` setting. An unset
  setting yields NULL and matches no rows — fail closed. ``organizations``
  itself is keyed on ``id`` (a tenant may see only its own org row).
  Superusers and ``BYPASSRLS`` roles bypass policies by PostgreSQL design;
  the application role must hold neither (doc 05 §3.2 layer 1 still applies).
- **Immutability triggers** (doc 06 §4, C2/INV-ROW-1): one generic
  ``BEFORE UPDATE`` trigger function rejects any change to a table's IMM
  columns (passed as trigger arguments), so ``organization_id`` and the other
  IMM columns are enforced in the database, not merely by convention.

Reversibility (INV-MIG-3): fully reversible — downgrade drops the five tables
(policies and triggers fall with them) and the trigger function.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

# The one immutability trigger function shared by all five tables. IMM column
# names arrive as trigger arguments (TG_ARGV), compared via to_jsonb so any
# column type (uuid, text, timestamptz, jsonb) is handled uniformly.
_IMM_FUNCTION_SQL = """
CREATE FUNCTION xiosync_forbid_imm_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    col text;
BEGIN
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF to_jsonb(OLD) -> col IS DISTINCT FROM to_jsonb(NEW) -> col THEN
            RAISE EXCEPTION
                'column "%" of table "%" is immutable (doc 06 §4)',
                col, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;
"""

# table -> IMM columns (doc 03 / model comments in persistence/models).
_IMM_COLUMNS: dict[str, tuple[str, ...]] = {
    "organizations": ("id", "slug", "created_at"),
    "actors": ("id", "organization_id", "config", "created_at"),
    "auth_identities": ("id", "organization_id", "human_actor_id", "created_at"),
    "memberships": ("id", "organization_id", "auth_identity_id", "created_at"),
    "sessions": ("id", "organization_id", "auth_identity_id", "created_at"),
}

# Tenant-bearing tables get the standard organization_id-keyed policy;
# organizations is keyed on its own id (doc 05 §3.2).
_RLS_KEY_COLUMN: dict[str, str] = {
    "organizations": "id",
    "actors": "organization_id",
    "auth_identities": "organization_id",
    "memberships": "organization_id",
    "sessions": "organization_id",
}


def _apply_rls_and_immutability() -> None:
    op.execute(_IMM_FUNCTION_SQL)
    for table, key_column in _RLS_KEY_COLUMN.items():
        predicate = f"{key_column} = current_setting('app.current_org', true)::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_{table}_org_isolation ON {table} "
            f"FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
        )
        imm_args = ", ".join(f"'{column}'" for column in _IMM_COLUMNS[table])
        op.execute(
            f"CREATE TRIGGER trg_{table}_forbid_imm_update "
            f"BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION xiosync_forbid_imm_update({imm_args})"
        )


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('active', 'suspended', 'archived')",
            name=op.f("ck_organizations_state_allowed"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
    )
    op.create_table(
        "actors",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_subtype", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("alias", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("lifecycle_phase", sa.Text(), nullable=False),
        sa.Column("trust_tier", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("runtime_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("health_status", sa.Text(), nullable=False),
        sa.Column("last_heartbeat", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "health_status IN ('healthy', 'degraded', 'offline', 'unknown')",
            name=op.f("ck_actors_health_status_allowed"),
        ),
        sa.CheckConstraint(
            "lifecycle_phase IN ('pre_birth', 'birth', 'operational', 'end_of_life')",
            name=op.f("ck_actors_lifecycle_phase_allowed"),
        ),
        sa.CheckConstraint(
            "trust_tier IN ('newcomer', 'contributor', 'trusted', 'core', 'admin')",
            name=op.f("ck_actors_trust_tier_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["actors.organization_id", "actors.id"],
            name="fk_actors_created_by_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "parent_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_actors_parent_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_actors_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_actors")),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_actors_organization_id_id")),
    )
    op.create_index(
        "ix_actors_org_type_state",
        "actors",
        ["organization_id", "actor_type", "state"],
        unique=False,
    )
    op.create_index(op.f("ix_actors_organization_id"), "actors", ["organization_id"], unique=False)
    op.create_table(
        "auth_identities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("human_actor_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('active', 'locked', 'disabled')",
            name=op.f("ck_auth_identities_state_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "human_actor_id"],
            ["actors.organization_id", "actors.id"],
            name="fk_auth_identities_human_actor_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_auth_identities_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_identities")),
        sa.UniqueConstraint("human_actor_id", name=op.f("uq_auth_identities_human_actor_id")),
        sa.UniqueConstraint(
            "organization_id", "email", name=op.f("uq_auth_identities_organization_id_email")
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_auth_identities_organization_id_id")
        ),
    )
    op.create_index(
        op.f("ix_auth_identities_organization_id"),
        "auth_identities",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("auth_identity_id", sa.UUID(), nullable=False),
        sa.Column("membership_role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "membership_role IN ('org_owner', 'org_admin', 'org_member', 'org_viewer')",
            name=op.f("ck_memberships_membership_role_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["auth_identity_id"],
            ["auth_identities.id"],
            name=op.f("fk_memberships_auth_identity_id_auth_identities"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_memberships_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint(
            "auth_identity_id",
            "organization_id",
            name=op.f("uq_memberships_auth_identity_id_organization_id"),
        ),
    )
    op.create_index(
        op.f("ix_memberships_organization_id"), "memberships", ["organization_id"], unique=False
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("auth_identity_id", sa.UUID(), nullable=False),
        sa.Column("refresh_token_hash", sa.Text(), nullable=False),
        sa.Column("access_token_jti", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('active', 'revoked', 'expired')", name=op.f("ck_sessions_state_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "auth_identity_id"],
            ["auth_identities.organization_id", "auth_identities.id"],
            name="fk_sessions_auth_identity_same_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_sessions_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_index(
        "ix_sessions_auth_identity_active",
        "sessions",
        ["auth_identity_id"],
        unique=False,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_index(
        op.f("ix_sessions_organization_id"), "sessions", ["organization_id"], unique=False
    )
    _apply_rls_and_immutability()


def downgrade() -> None:
    # Policies and triggers are dropped with their tables; only the shared
    # trigger function needs an explicit drop (after the tables that use it).
    op.drop_index(op.f("ix_sessions_organization_id"), table_name="sessions")
    op.drop_index(
        "ix_sessions_auth_identity_active",
        table_name="sessions",
        postgresql_where=sa.text("state = 'active'"),
    )
    op.drop_table("sessions")
    op.drop_index(op.f("ix_memberships_organization_id"), table_name="memberships")
    op.drop_table("memberships")
    op.drop_index(op.f("ix_auth_identities_organization_id"), table_name="auth_identities")
    op.drop_table("auth_identities")
    op.drop_index(op.f("ix_actors_organization_id"), table_name="actors")
    op.drop_index("ix_actors_org_type_state", table_name="actors")
    op.drop_table("actors")
    op.drop_table("organizations")
    op.execute("DROP FUNCTION xiosync_forbid_imm_update()")
