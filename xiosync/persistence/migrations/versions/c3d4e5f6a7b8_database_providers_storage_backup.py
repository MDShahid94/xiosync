"""Add database_providers + storage_providers backup columns

Creates the database_providers table (secondary DB / backup DB registry)
and adds is_backup + backup_for_id columns to storage_providers to
support multi-storage fan-out writes.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = '0046'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. storage_providers: add backup metadata columns ─────────────────────
    # is_backup=True means writes to this provider are fan-out copies (not primary)
    # backup_for_id references the primary provider this one backs up
    op.add_column('storage_providers',
        sa.Column('is_backup', sa.Boolean(), nullable=False,
                  server_default='false'))
    op.add_column('storage_providers',
        sa.Column('backup_for_id', sa.UUID(), nullable=True))
    op.add_column('storage_providers',
        sa.Column('notebook_file_id', sa.Text(), nullable=True,
                  comment='Drive file ID of the Colab worker notebook (for bootstrap URL)'))

    op.create_foreign_key(
        'fk_storage_providers_backup_for',
        'storage_providers', 'storage_providers',
        ['backup_for_id'], ['id'],
        ondelete='SET NULL',
    )

    # ── 2. database_providers table ───────────────────────────────────────────
    # Allows registering secondary/backup databases (Supabase, external Postgres,
    # Turso, Neon, etc.) for cross-database backup and multi-DB fan-out.
    op.create_table(
        'database_providers',
        sa.Column('id', sa.UUID(), nullable=False,
                  server_default=sa.text('gen_random_uuid()'),
                  primary_key=True),
        sa.Column('organization_id', sa.UUID(), nullable=True,
                  comment='NULL = platform-global; set = org-private'),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('provider_type', sa.Text(), nullable=False,
                  comment='postgres | mysql | sqlite | turso | neon | supabase | custom'),
        sa.Column('connection_config', sa.dialects.postgresql.JSONB(), nullable=False,
                  server_default='{}',
                  comment='Non-secret config: host, port, database, account_id, ...'),
        sa.Column('vault_key', sa.Text(), nullable=True,
                  comment='Key in vaulted_secrets holding the connection credential'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('is_backup', sa.Boolean(), nullable=False, server_default='false',
                  comment='True = writes are fan-out copies (not primary)'),
        sa.Column('backup_for_id', sa.UUID(), nullable=True,
                  comment='References the primary database_providers.id this backs up'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100',
                  comment='Lower = higher priority. Primary=1, secondary=50, cold=100'),
        sa.Column('is_writable', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('sync_enabled', sa.Boolean(), nullable=False, server_default='false',
                  comment='If true, XIOSYNC will periodically sync objects to this DB'),
        sa.Column('last_synced_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('last_sync_status', sa.Text(), nullable=True),
        sa.Column('sync_stats', sa.dialects.postgresql.JSONB(), nullable=False,
                  server_default='{}'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )

    op.create_foreign_key(
        'fk_database_providers_org',
        'database_providers', 'organizations',
        ['organization_id'], ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'fk_database_providers_backup_for',
        'database_providers', 'database_providers',
        ['backup_for_id'], ['id'],
        ondelete='SET NULL',
    )

    # Unique: org-scoped providers unique by name
    op.create_unique_constraint(
        'uq_database_providers_org_name',
        'database_providers', ['organization_id', 'name'],
    )
    # Unique: platform-global providers unique by name (NULL org)
    op.create_index(
        'uq_database_provider_platform_name', 'database_providers', ['name'],
        unique=True,
        postgresql_where=sa.text('organization_id IS NULL'),
    )
    op.create_index(
        'ix_database_providers_org', 'database_providers', ['organization_id'],
        postgresql_where=sa.text('organization_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_database_providers_org', 'database_providers')
    op.drop_index('uq_database_provider_platform_name', 'database_providers')
    op.drop_constraint('uq_database_providers_org_name', 'database_providers')
    op.drop_constraint('fk_database_providers_backup_for', 'database_providers')
    op.drop_constraint('fk_database_providers_org', 'database_providers')
    op.drop_table('database_providers')

    op.drop_constraint('fk_storage_providers_backup_for', 'storage_providers')
    op.drop_column('storage_providers', 'notebook_file_id')
    op.drop_column('storage_providers', 'backup_for_id')
    op.drop_column('storage_providers', 'is_backup')
