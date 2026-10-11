"""Create federated_credentials table and make password_hash nullable.

Revision ID: 0059_federated_credentials
Revises: 0058_edge_properties
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0059_federated_credentials"
down_revision = "0058_edge_properties"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make password_hash nullable for federated-only users
    op.alter_column("member_auth", "password_hash", nullable=True)

    op.create_table(
        "federated_credentials",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("member_auth_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("provider_subject_id", sa.Text, nullable=False),
        sa.Column("provider_email", sa.Text, nullable=True),
        sa.Column("provider_display_name", sa.Text, nullable=True),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'active'")),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "provider", "provider_subject_id", name="uq_federated_provider_subject"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "member_auth_id"],
            ["member_auth.organization_id", "member_auth.id"],
            name="fk_federated_auth_same_org",
        ),
        sa.CheckConstraint(
            "provider IN ('google', 'github', 'microsoft', 'okta', 'saml', 'oidc_custom')",
            name="ck_federated_provider_allowed",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'revoked')",
            name="ck_federated_state_allowed",
        ),
    )
    op.create_index(
        "ix_federated_member_auth", "federated_credentials", ["member_auth_id"]
    )
    op.create_index(
        "ix_federated_provider_lookup",
        "federated_credentials",
        ["provider", "provider_subject_id"],
    )


def downgrade() -> None:
    op.drop_table("federated_credentials")
    op.alter_column("member_auth", "password_hash", nullable=False)
