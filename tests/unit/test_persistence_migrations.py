"""Unit tests for Alembic migration hooks and protocol evolution logging."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from alembic.runtime.migration import MigrationContext, MigrationInfo
from xiosync.persistence.migrations.env import (
    _database_url,
    _on_version_apply,
    run_migrations_online,
    target_metadata,
)


def _make_migration_info(
    *,
    is_upgrade: bool = True,
    is_stamp: bool = False,
    up_revisions: str | tuple[str, ...] = "0002_rev",
    down_revisions: str | tuple[str, ...] = "0001_rev",
) -> MigrationInfo:
    rev_map = MagicMock()
    return MigrationInfo(
        revision_map=rev_map,
        is_upgrade=is_upgrade,
        is_stamp=is_stamp,
        up_revisions=up_revisions,
        down_revisions=down_revisions,
    )


def test_on_version_apply_logs_upgrade(caplog: pytest.LogCaptureFixture) -> None:
    ctx = MagicMock(spec=MigrationContext)
    step = _make_migration_info(
        is_upgrade=True,
        up_revisions="0002_identity_tables",
        down_revisions="0001_baseline",
    )

    with caplog.at_level(logging.INFO, logger="xiosync.persistence.migrations"):
        _on_version_apply(
            ctx=ctx,
            step=step,
            heads={"0002_identity_tables"},
            run_args={},
        )

    assert "protocol.evolution: schema_migration applied" in caplog.text
    assert "direction=upgrade" in caplog.text
    assert "revision=0002_identity_tables" in caplog.text
    assert "0001_baseline -> 0002_identity_tables" in caplog.text


def test_on_version_apply_logs_downgrade(caplog: pytest.LogCaptureFixture) -> None:
    ctx = MagicMock(spec=MigrationContext)
    step = _make_migration_info(
        is_upgrade=False,
        up_revisions="0002_identity_tables",
        down_revisions="0001_baseline",
    )

    with caplog.at_level(logging.INFO, logger="xiosync.persistence.migrations"):
        _on_version_apply(
            ctx=ctx,
            step=step,
            heads={"0001_baseline"},
            run_args={},
        )

    assert "protocol.evolution: schema_migration applied" in caplog.text
    assert "direction=downgrade" in caplog.text
    assert "revision=0002_identity_tables" in caplog.text
    assert "0002_identity_tables -> 0001_baseline" in caplog.text


def test_on_version_apply_root_migration(caplog: pytest.LogCaptureFixture) -> None:
    ctx = MagicMock(spec=MigrationContext)
    step = _make_migration_info(
        is_upgrade=True,
        up_revisions="0001_baseline",
        down_revisions=(),
    )

    with caplog.at_level(logging.INFO, logger="xiosync.persistence.migrations"):
        _on_version_apply(
            ctx=ctx,
            step=step,
            heads={"0001_baseline"},
            run_args={},
        )

    assert "protocol.evolution: schema_migration applied" in caplog.text
    assert "direction=upgrade" in caplog.text
    assert "base -> 0001_baseline" in caplog.text


def test_database_url_requires_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        _database_url()


def test_run_migrations_online_configures_on_version_apply() -> None:
    mock_engine = MagicMock()
    mock_connection = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = mock_connection

    with (
        patch("xiosync.persistence.migrations.env.create_engine", return_value=mock_engine),
        patch("xiosync.persistence.migrations.env._database_url", return_value="postgresql+psycopg://u:p@localhost/db"),
        patch("xiosync.persistence.migrations.env.context") as mock_context,
    ):
        run_migrations_online()

        mock_context.configure.assert_called_once_with(
            connection=mock_connection,
            target_metadata=target_metadata,
            on_version_apply=_on_version_apply,
        )
        mock_context.run_migrations.assert_called_once()
        mock_engine.dispose.assert_called_once()
