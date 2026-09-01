"""Application lifecycle and readiness management (Phase 7 Step 1).

Normative references:
- Doc 09 §2 (M5): Fail-fast startup with strict config validation
- Doc 09 §3 (M7): Distinct /live and /ready endpoints
- Doc 04 §5 (C6): Migration-as-deploy-step with readiness head-gate

Responsibilities:
1. Verify database connectivity at startup
2. Check that migrations are at head revision (fail startup if not)
3. Provide /live endpoint (immediate 200, process is running)
4. Provide /ready endpoint (200 only if db connected and migrations at head)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Alembic script location relative to the project root — matches
# ``alembic.ini`` ``script_location = %(here)s/xiosync/persistence/migrations``.
# XIOPATH/alembic is the read-only LEGACY directory; never reference it here.
_ALEMBIC_SCRIPT_LOCATION = "xiosync/persistence/migrations"


class MigrationNotAtHeadError(RuntimeError):
    """Database migrations are not at the head revision (C6)."""


class DatabaseConnectionError(RuntimeError):
    """Cannot connect to database or perform required checks."""


@dataclass(frozen=True, slots=True)
class ReadinessState:
    """Immutable snapshot of application readiness state (M7)."""

    is_live: bool
    """True if the process is running (always True in production)."""

    is_ready: bool
    """True if database is connected and migrations are at head."""

    live_reason: str
    """Rationale for is_live status."""

    ready_reason: str
    """Rationale for is_ready status (empty if ready)."""


def get_alembic_head_revision(alembic_dir: str) -> str:
    """Get the head revision identifier from the Alembic script directory.

    Args:
        alembic_dir: Path to Alembic script directory (e.g.,
            ``"xiosync/persistence/migrations"`` — the XIOSYNC chain).

    Returns:
        The head revision string (e.g., ``"0009_sandboxed_plugins"`` rev hash).

    Raises:
        RuntimeError: If the Alembic configuration cannot be read.
    """
    try:
        script_dir = ScriptDirectory(alembic_dir)
        head_revision = script_dir.get_current_head()
        if not head_revision:
            raise RuntimeError("No migration revisions found in Alembic script directory")
        return head_revision
    except Exception as exc:
        raise RuntimeError(f"Failed to read Alembic script directory: {exc}") from exc


def get_database_current_revision(engine: Engine) -> str | None:
    """Get the current schema revision applied to the database.

    Args:
        engine: SQLAlchemy engine connected to the database.

    Returns:
        The current revision string, or None if migrations table doesn't exist.

    Raises:
        DatabaseConnectionError: If the database query fails.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(
                # Standard Alembic single-head chain: alembic_version has
                # exactly one row (version_num only — no applied column).
                text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            row = result.fetchone()
            return row[0] if row else None
    except Exception as exc:
        # Include the table not existing as one of the possible reasons for failure
        raise DatabaseConnectionError(
            f"Failed to query database (alembic_version table may not exist): {exc}"
        ) from exc


def verify_database_connection(engine: Engine) -> None:
    """Verify that the database is reachable and responsive.

    Args:
        engine: SQLAlchemy engine to test.

    Raises:
        DatabaseConnectionError: If the database cannot be reached.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise DatabaseConnectionError(f"Database connection failed: {exc}") from exc


def verify_migrations_at_head(engine: Engine, alembic_dir: str = _ALEMBIC_SCRIPT_LOCATION) -> None:
    """Verify that database migrations are at the head revision (C6).

    This function enforces that:
    1. The database is reachable
    2. The alembic_version table exists
    3. The current schema revision matches the head revision from the script directory

    Startup must fail fast if this check fails (M5, INV-STARTUP-1).

    Args:
        engine: SQLAlchemy engine to the database.
        alembic_dir: Path to Alembic script directory.

    Raises:
        MigrationNotAtHeadError: If migrations are not at head (forces startup to fail).
        DatabaseConnectionError: If database connectivity fails.
    """
    verify_database_connection(engine)

    head_revision = get_alembic_head_revision(alembic_dir)
    current_revision = get_database_current_revision(engine)

    if current_revision is None:
        raise MigrationNotAtHeadError(
            f"Database has no applied migrations. Head revision is {head_revision}. "
            "Run migrations with: alembic upgrade head"
        )

    if current_revision != head_revision:
        raise MigrationNotAtHeadError(
            f"Database schema is at revision {current_revision}, "
            f"but head revision is {head_revision}. "
            f"Run migrations with: alembic upgrade head"
        )

    logger.info(f"Database migrations verified at head revision: {head_revision}")


def check_readiness(engine: Engine, alembic_dir: str = _ALEMBIC_SCRIPT_LOCATION) -> ReadinessState:
    """Check application readiness status (M7).

    Returns a ReadinessState indicating:
    - is_live: Always True (process is running)
    - is_ready: True only if database is connected and migrations are at head

    Args:
        engine: SQLAlchemy engine to the database.
        alembic_dir: Path to Alembic script directory.

    Returns:
        ReadinessState with current status and reasoning.
    """
    is_live = True
    live_reason = "Process is running"

    ready_reason = ""
    try:
        verify_migrations_at_head(engine, alembic_dir)
        is_ready = True
    except (MigrationNotAtHeadError, DatabaseConnectionError) as exc:
        is_ready = False
        ready_reason = str(exc)

    return ReadinessState(
        is_live=is_live,
        is_ready=is_ready,
        live_reason=live_reason,
        ready_reason=ready_reason,
    )
