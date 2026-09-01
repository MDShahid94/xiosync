"""Unit tests for PostgreSQL-only engine wiring (doc 06 §2, C6; D-013)."""

from __future__ import annotations

import pytest
from xiosync.persistence.database import (
    UnsupportedDatabaseError,
    create_database_engine,
    validated_psycopg_url,
)


def test_psycopg_url_passes_through_unchanged() -> None:
    url = "postgresql+psycopg://user:pw@host:5432/xiosync"
    assert validated_psycopg_url(url) == url


def test_plain_postgresql_url_is_pinned_to_psycopg() -> None:
    assert (
        validated_psycopg_url("postgresql://user:pw@host:5432/xiosync")
        == "postgresql+psycopg://user:pw@host:5432/xiosync"
    )


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///data/xiopath.db",  # the C6 pattern, forever rejected
        "postgresql+psycopg2://user:pw@host/db",  # wrong driver
        "postgresql+asyncpg://user:pw@host/db",  # wrong driver
        "mysql://user:pw@host/db",
        "not-a-url",
        "",
    ],
)
def test_non_postgres_or_non_psycopg_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsupportedDatabaseError):
        validated_psycopg_url(url)


def test_engine_factory_pins_psycopg_dialect_without_connecting() -> None:
    engine = create_database_engine("postgresql://user:pw@host:5432/xiosync")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
    finally:
        engine.dispose()
