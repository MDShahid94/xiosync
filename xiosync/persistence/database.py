"""PostgreSQL-only engine wiring (C6 remediation; doc 06 §2, D-013).

The only supported engine is PostgreSQL and the only supported driver is
psycopg 3. ``postgresql://`` URLs are normalized to ``postgresql+psycopg://``
so SQLAlchemy can never fall back to another DBAPI. No SQLite path exists and
none may ever be added (INV-DB-1, doc 06 §11).

Schema authority note (INV-SCHEMA-1): nothing in this module — or anywhere in
application code — issues DDL. ``xiosync/persistence/migrations/`` is the single
schema authority.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_POSTGRES_SCHEME = "postgresql"
_PSYCOPG_SCHEME = "postgresql+psycopg"


class UnsupportedDatabaseError(ValueError):
    """A non-PostgreSQL/non-psycopg database URL was supplied (C6)."""


def validated_psycopg_url(url: str) -> str:
    """Validate ``url`` targets PostgreSQL and pin the psycopg 3 driver.

    Accepts ``postgresql://`` (normalized to ``postgresql+psycopg://``) and
    ``postgresql+psycopg://`` (returned unchanged). Every other scheme —
    including SQLite and any alternative PostgreSQL driver — is rejected.
    """
    scheme, separator, rest = url.partition("://")
    if not separator:
        raise UnsupportedDatabaseError("DATABASE_URL is not a URL (expected '<scheme>://...')")
    if scheme == _PSYCOPG_SCHEME:
        return url
    if scheme == _POSTGRES_SCHEME:
        return f"{_PSYCOPG_SCHEME}://{rest}"
    raise UnsupportedDatabaseError(
        f"DATABASE_URL scheme {scheme!r} is not supported: PostgreSQL via "
        f"psycopg is the only engine/driver (doc 06 INV-DB-1, D-013)"
    )


def create_database_engine(database_url: str) -> Engine:
    """Create the process's SQLAlchemy engine from a validated URL.

    ``pool_pre_ping`` guards serverless/pooled PostgreSQL endpoints that drop
    idle connections. The engine never applies schema (INV-SCHEMA-2).

    P10: Pool parameters are configurable via environment variables:
    - ``DB_POOL_SIZE`` (default 5)
    - ``DB_MAX_OVERFLOW`` (default 10)
    - ``DB_POOL_TIMEOUT`` (default 30)
    - ``DB_POOL_RECYCLE`` (default -1 = no recycle)
    """
    import os

    return create_engine(
        validated_psycopg_url(database_url),
        pool_pre_ping=True,
        pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
        pool_timeout=int(os.environ.get("DB_POOL_TIMEOUT", "30")),
        pool_recycle=int(os.environ.get("DB_POOL_RECYCLE", "-1")),
    )
