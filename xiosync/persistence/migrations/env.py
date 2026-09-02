"""Alembic environment — the single migration chain's runtime (doc 06 §3).

Rules enforced here:
- The URL comes ONLY from the ``DATABASE_URL`` environment variable, validated
  and pinned to PostgreSQL + psycopg (C6; doc 09 §1). ``alembic.ini`` carries
  no URL and no fallback exists.
- ``target_metadata`` is the (currently empty) declarative metadata; the
  autogenerate-diff-must-be-empty CI gate (INV-TEST-SCHEMA-2) compares against
  it.
- This process is a discrete migration job. The API process never imports or
  executes this module (INV-MIG-1).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Collection, Mapping
from typing import Any

from alembic import context
from alembic.runtime.migration import MigrationContext, MigrationInfo
from sqlalchemy import create_engine
from xiosync.persistence.database import validated_psycopg_url
from xiosync.persistence.models import Base

logger = logging.getLogger("xiosync.persistence.migrations")

# The declarative metadata (persistence/models). The autogenerate-diff-must-
# be-empty CI gate (INV-TEST-SCHEMA-2) compares the migrated database against
# exactly this object.
target_metadata = Base.metadata


def _on_version_apply(
    ctx: MigrationContext,
    step: MigrationInfo,
    heads: Collection[Any],
    run_args: Mapping[str, Any],
) -> None:
    """Record a schema migration as an observable protocol evolution event."""
    direction = "upgrade" if step.is_upgrade else "downgrade"
    source = ",".join(step.source_revision_ids) if step.source_revision_ids else "base"
    destination = (
        ",".join(step.destination_revision_ids)
        if step.destination_revision_ids
        else "base"
    )
    revision = step.up_revision_id or destination
    logger.info(
        "protocol.evolution: schema_migration applied direction=%s revision=%s (%s -> %s)",
        direction,
        revision,
        source,
        destination,
    )


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set: migrations require an explicit "
            "PostgreSQL URL (doc 09 §1; no embedded defaults — L4)"
        )
    return validated_psycopg_url(url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (``alembic upgrade --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database (the deploy-step path)."""
    engine = create_engine(_database_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            on_version_apply=_on_version_apply,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


try:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
except NameError:
    # Invoked outside Alembic runner (e.g., imported for unit testing)
    pass
