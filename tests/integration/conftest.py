"""Integration-test fixtures — scratch database per doc 06 §10.

Every integration test runs against a freshly created, empty PostgreSQL
database (INV-TEST-SCHEMA-1) reached only via ``DATABASE_URL`` (D-016: Neon
Postgres in-sandbox, ``postgres:17`` service containers in CI). The fixture:

1. connects to the database named in ``DATABASE_URL`` as an admin channel,
2. ``CREATE DATABASE`` a uniquely named scratch database,
3. yields a URL pointing at that scratch database, and
4. drops it afterwards (``WITH (FORCE)`` to evict stray connections).

The ``CREATE DATABASE`` here is test *infrastructure* (a blank container for
the real Alembic chain), not schema fabrication: INV-TEST-SCHEMA-1 bans
fabricated **tables**; the schema itself may only ever come from
``xiosync/persistence/migrations/`` (INV-SCHEMA-1).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from xiosync.domain.event_registry import event_registry
from xiosync.persistence.database import validated_psycopg_url
from xiosync.services.bootstrap import _CORE_EVENT_TYPES

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Deterministic throwaway credential for the per-test application role. It
# authenticates only against the per-test scratch database created below and
# the role is dropped in teardown — it protects nothing and is not a secret.
_APP_ROLE_PASSWORD = "xiosync-integration-test"


@contextmanager
def database_url_env(url: str) -> Iterator[None]:
    """Temporarily point ``DATABASE_URL`` (env.py's only source) at ``url``."""
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if original is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = original


def _admin_url() -> str:
    """The operator-supplied PostgreSQL URL, or skip if none is configured."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        pytest.skip(
            "DATABASE_URL is not set — integration tests require a real "
            "PostgreSQL database (doc 06 §10; D-016). CI MUST set it: a "
            "skip here is only acceptable for local unit-only runs."
        )
    return validated_psycopg_url(url)


@pytest.fixture()
def scratch_database_url() -> Iterator[str]:
    """Yield a URL to a freshly created, empty scratch database, then drop it."""
    admin_url = make_url(_admin_url())
    scratch_name = f"xiosync_test_{uuid.uuid4().hex}"

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            # scratch_name is generated from uuid4 hex above — not user input.
            connection.execute(text(f'CREATE DATABASE "{scratch_name}"'))
            # Disable autovacuum for scratch databases. Migration 0025 generates enough
            # DDL to trigger immediate autovacuum on the newly constrained tables.
            # Because autovacuum runs as the `postgres` superuser, the test role
            # (xiosync_test) gets InsufficientPrivilege when WITH (FORCE) tries to
            # terminate it during teardown.
            connection.execute(text(f'ALTER DATABASE "{scratch_name}" SET autovacuum = off'))

        try:
            yield admin_url.set(database=scratch_name).render_as_string(hide_password=False)
        finally:
            with admin_engine.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{scratch_name}" WITH (FORCE)'))
    finally:
        admin_engine.dispose()


@pytest.fixture()
def migrated_database_url(scratch_database_url: str) -> str:
    """A scratch database migrated to the chain's head via Alembic only.

    The schema comes exclusively from ``xiosync/persistence/migrations/``
    (INV-SCHEMA-1); this fixture issues no DDL of its own.
    """
    with database_url_env(scratch_database_url):
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
    return scratch_database_url


@pytest.fixture()
def app_role_database_url(migrated_database_url: str) -> Iterator[str]:
    """URL connecting to the migrated scratch DB as a plain application role.

    RLS is invisible to superusers and ``BYPASSRLS`` roles by PostgreSQL
    design, so proving isolation (doc 05 §3.2) requires a role with neither.
    Role creation here is test *infrastructure* (like the scratch database),
    not schema fabrication — no table DDL is issued (INV-TEST-SCHEMA-1).
    """
    url = make_url(migrated_database_url)
    role_name = f"xiosync_app_{uuid.uuid4().hex}"

    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            # role_name is generated from uuid4 hex above — not user input.
            connection.execute(
                text(
                    f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD '{_APP_ROLE_PASSWORD}' "
                    "NOSUPERUSER NOBYPASSRLS"
                )
            )
            # In PG 16+, CREATEROLE gives ADMIN on the created role but NOT
            # membership.  DROP OWNED BY (used in teardown) requires the caller
            # to be a *member* of the role.  Granting the new role to the admin
            # session satisfies this; the grant is automatically cleaned up when
            # the role is dropped in teardown.
            connection.execute(text(f'GRANT "{role_name}" TO CURRENT_USER'))
            connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role_name}"'))
            connection.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    f'IN SCHEMA public TO "{role_name}"'
                )
            )
        try:
            yield url.set(username=role_name, password=_APP_ROLE_PASSWORD).render_as_string(
                hide_password=False
            )
        finally:
            with engine.connect() as connection:
                connection.execute(text(f'DROP OWNED BY "{role_name}"'))
                connection.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def seeded_event_registry() -> Iterator[None]:
    """Populate the process-global event_registry before each integration test.

    Integration tests skip genesis (by design — INV-TEST-SCHEMA-1), so the
    event_registry singleton is never populated from the DB.  Without seeding,
    ``event_registry.is_valid()`` returns ``True`` for *every* string, making
    ``EventService.append`` accept unknown types and breaking
    ``test_append_unknown_event_type_is_rejected``.

    This fixture seeds the registry with the same ``_CORE_EVENT_TYPES`` that
    ``BootstrapService._seed_type_registry`` would register at genesis, then
    resets it on teardown to prevent cross-test contamination.
    """
    event_registry.register([value for value, _desc in _CORE_EVENT_TYPES])
    try:
        yield
    finally:
        event_registry.reset()
