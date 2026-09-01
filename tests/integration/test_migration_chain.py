"""Empty-DB migration harness — the whole chain must round-trip (doc 06 §10).

Proves, against a real empty PostgreSQL database:

- the chain has exactly one head (INV-MIG-2),
- ``upgrade → downgrade → upgrade`` succeeds over the WHOLE chain
  (INV-MIG-3, INV-TEST-SCHEMA-2), and
- the schema the tests see comes only from Alembic — the harness never
  issues its own DDL (INV-TEST-SCHEMA-1, INV-SCHEMA-1).

The Alembic config is the repo-root ``alembic.ini``; ``env.py`` reads the URL
exclusively from ``DATABASE_URL``, so the harness points that variable at the
scratch database for the duration of each run.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

pytestmark = pytest.mark.integration


def _alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


@contextmanager
def _database_url_env(url: str) -> Iterator[None]:
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


def _current_revision(database_url: str) -> str | None:
    """Read ``alembic_version`` directly; None when absent (empty DB / base)."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                return None
            row = connection.execute(text("SELECT version_num FROM alembic_version")).one_or_none()
            return None if row is None else str(row[0])
    finally:
        engine.dispose()


def _single_head(config: Config) -> str:
    """The chain's one head; fails loudly if the chain ever branches."""
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"chain must have exactly one head (INV-MIG-2), got {heads}"
    return heads[0]


def test_chain_has_exactly_one_head() -> None:
    """INV-MIG-2: one linear chain, no branching heads."""
    _single_head(_alembic_config())


def test_upgrade_downgrade_upgrade_roundtrip(scratch_database_url: str) -> None:
    """INV-MIG-3 / INV-TEST-SCHEMA-2: full-chain round-trip from an empty DB."""
    config = _alembic_config()
    head = _single_head(config)

    assert _current_revision(scratch_database_url) is None, (
        "scratch database must start empty (INV-TEST-SCHEMA-1)"
    )

    with _database_url_env(scratch_database_url):
        command.upgrade(config, "head")
        assert _current_revision(scratch_database_url) == head

        command.downgrade(config, "base")
        assert _current_revision(scratch_database_url) is None

        command.upgrade(config, "head")
        assert _current_revision(scratch_database_url) == head
