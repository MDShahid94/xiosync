"""0018 — document collections and pages tables.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_ts = TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "document_collections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("doc_type", sa.Text, nullable=False, server_default="custom"),
        sa.Column("description", sa.Text),
        sa.Column("version", sa.Text, nullable=False, server_default="1.0.0"),
        sa.Column("state", sa.Text, nullable=False, server_default="draft"),
        sa.Column("created_by", UUID(as_uuid=True), nullable=False),
        sa.Column("published_at", _ts),
        sa.Column("created_at", _ts, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _ts),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("state IN ('draft','published','archived','deprecated')", name="ck_doc_collections_state"),
        sa.UniqueConstraint("organization_id", "slug", "version", name="uq_doc_collections_org_slug_version"),
    )

    op.create_table(
        "document_pages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("collection_id", UUID(as_uuid=True), sa.ForeignKey("document_collections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("artifact_id", UUID(as_uuid=True), sa.ForeignKey("artifacts.id")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("content_format", sa.Text, nullable=False, server_default="markdown"),
        sa.Column("inline_content", sa.Text),
        sa.Column("page_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("parent_page_id", UUID(as_uuid=True), sa.ForeignKey("document_pages.id")),
        sa.Column("depth", sa.Integer, nullable=False, server_default="0"),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", _ts, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _ts),
        sa.UniqueConstraint("collection_id", "slug", name="uq_doc_pages_collection_slug"),
        sa.Index("ix_doc_pages_parent", "collection_id", "parent_page_id"),
        sa.Index("ix_doc_pages_order", "collection_id", "page_order"),
    )

    # RLS policies for multi-tenant isolation
    op.execute("ALTER TABLE document_collections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_pages ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation_doc_collections ON document_collections
        USING (organization_id = current_setting('app.current_org_id')::uuid)
    """)
    op.execute("""
        CREATE POLICY tenant_isolation_doc_pages ON document_pages
        USING (organization_id = current_setting('app.current_org_id')::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_doc_pages ON document_pages")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_doc_collections ON document_collections")
    op.drop_table("document_pages")
    op.drop_table("document_collections")
