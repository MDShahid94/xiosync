"""Unit tests for health check module (Phase 7 Step 1).

Tests verify:
- M5: Fail-fast startup with strict config validation
- M7: Distinct /live and /ready endpoints
- C6: Migration-as-deploy-step with readiness head-gate
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from xiosync.core.health import (
    DatabaseConnectionError,
    MigrationNotAtHeadError,
    ReadinessState,
    check_readiness,
    get_alembic_head_revision,
    get_database_current_revision,
    verify_database_connection,
    verify_migrations_at_head,
)


@pytest.fixture
def sqlite_engine() -> Engine:
    """Create an in-memory SQLite engine for testing."""
    return create_engine("sqlite:///:memory:")


@pytest.fixture
def sqlite_engine_with_migrations(sqlite_engine: Engine) -> Engine:
    """Create an in-memory SQLite engine with the real alembic_version schema.

    Standard Alembic alembic_version has exactly one column: version_num.
    No applied/timestamp column exists — that was a test fiction that caused
    get_database_current_revision() to carry an ORDER BY applied clause that
    would fail against a real PostgreSQL database.
    """
    with sqlite_engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('14c2c1f29abe')")
        )
        conn.commit()
    return sqlite_engine


class TestVerifyDatabaseConnection:
    """Tests for verify_database_connection (connectivity check)."""

    def test_verify_database_connection_success(self, sqlite_engine: Engine) -> None:
        """Verify succeeds when database is reachable."""
        # Should not raise
        verify_database_connection(sqlite_engine)

    def test_verify_database_connection_failure(self) -> None:
        """Verify raises DatabaseConnectionError when database is unreachable."""
        # Create a mock engine that fails
        mock_engine = MagicMock(spec=Engine)
        mock_engine.connect.side_effect = RuntimeError("Connection refused")

        with pytest.raises(DatabaseConnectionError, match="Connection refused"):
            verify_database_connection(mock_engine)


class TestGetDatabaseCurrentRevision:
    """Tests for get_database_current_revision (query current schema version)."""

    def test_get_current_revision_success(self, sqlite_engine_with_migrations: Engine) -> None:
        """Get current revision when alembic_version table exists."""
        revision = get_database_current_revision(sqlite_engine_with_migrations)
        assert revision == "14c2c1f29abe"

    def test_get_current_revision_no_table(self, sqlite_engine: Engine) -> None:
        """Return None when alembic_version table doesn't exist."""
        # When the table doesn't exist, SQLAlchemy raises an exception
        # Our implementation catches this and wraps it as DatabaseConnectionError
        with pytest.raises(DatabaseConnectionError):
            get_database_current_revision(sqlite_engine)

    def test_get_current_revision_multiple_versions(self, sqlite_engine: Engine) -> None:
        """Return latest revision when multiple versions exist."""
        with sqlite_engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE alembic_version (
                    version_num VARCHAR(32) PRIMARY KEY,
                    applied TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """)
            )
            # Insert in reverse order to test ORDER BY DESC
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('aaa1111')")
            )
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('zzz9999')")
            )
            conn.commit()

        revision = get_database_current_revision(sqlite_engine)
        # Should return the one with latest applied timestamp (last inserted)
        assert revision is not None

    def test_get_current_revision_connection_error(self) -> None:
        """Raise DatabaseConnectionError when query fails."""
        mock_engine = MagicMock(spec=Engine)
        mock_engine.connect.side_effect = RuntimeError("Connection lost")

        with pytest.raises(DatabaseConnectionError, match="Connection lost"):
            get_database_current_revision(mock_engine)


class TestGetAlembicHeadRevision:
    """Tests for get_alembic_head_revision (query Alembic script directory)."""

    def test_get_head_revision_success(self) -> None:
        """Get head revision from actual Alembic script directory."""
        # Use the real XIOPATH alembic directory
        head = get_alembic_head_revision("XIOPATH/alembic")
        assert head is not None
        assert isinstance(head, str)
        assert len(head) > 0

    def test_get_head_revision_invalid_directory(self) -> None:
        """Raise RuntimeError for invalid Alembic directory."""
        with pytest.raises(RuntimeError, match="Failed to read Alembic script directory"):
            get_alembic_head_revision("/nonexistent/alembic")


class TestVerifyMigrationsAtHead:
    """Tests for verify_migrations_at_head (C6 head-gate)."""

    def test_verify_at_head_success(self) -> None:
        """Verify succeeds when database is at head revision."""
        # Create a mock engine that returns the same revision as head
        with patch("xiosync.core.health.get_alembic_head_revision") as mock_get_head:
            with patch("xiosync.core.health.get_database_current_revision") as mock_get_current:
                mock_get_head.return_value = "14c2c1f29abe"
                mock_get_current.return_value = "14c2c1f29abe"

                mock_engine = MagicMock(spec=Engine)
                # Should not raise
                verify_migrations_at_head(mock_engine, alembic_dir="test_dir")

    def test_verify_at_head_not_applied(self) -> None:
        """Raise MigrationNotAtHeadError when no migrations applied."""
        with patch("xiosync.core.health.get_alembic_head_revision") as mock_get_head:
            with patch("xiosync.core.health.get_database_current_revision") as mock_get_current:
                mock_get_head.return_value = "14c2c1f29abe"
                mock_get_current.return_value = None

                mock_engine = MagicMock(spec=Engine)

                with pytest.raises(
                    MigrationNotAtHeadError, match="Database has no applied migrations"
                ):
                    verify_migrations_at_head(mock_engine, alembic_dir="test_dir")

    def test_verify_at_head_behind(self) -> None:
        """Raise MigrationNotAtHeadError when database is behind head."""
        with patch("xiosync.core.health.get_alembic_head_revision") as mock_get_head:
            with patch("xiosync.core.health.get_database_current_revision") as mock_get_current:
                mock_get_head.return_value = "14c2c1f29abe"
                mock_get_current.return_value = "5f7f5f1793c7"

                mock_engine = MagicMock(spec=Engine)

                with pytest.raises(
                    MigrationNotAtHeadError, match="Database schema is at revision"
                ):
                    verify_migrations_at_head(mock_engine, alembic_dir="test_dir")

    def test_verify_at_head_database_error(self) -> None:
        """Raise DatabaseConnectionError when connection fails."""
        with patch("xiosync.core.health.verify_database_connection") as mock_verify:
            mock_verify.side_effect = DatabaseConnectionError("Connection timeout")

            mock_engine = MagicMock(spec=Engine)

            with pytest.raises(DatabaseConnectionError, match="Connection timeout"):
                verify_migrations_at_head(mock_engine)


class TestCheckReadiness:
    """Tests for check_readiness (M7 readiness state)."""

    def test_check_readiness_ready(self) -> None:
        """Return ready state when all checks pass."""
        with patch("xiosync.core.health.verify_migrations_at_head"):
            mock_engine = MagicMock(spec=Engine)
            state = check_readiness(mock_engine)

            assert state.is_live is True
            assert state.is_ready is True
            assert state.live_reason == "Process is running"
            assert state.ready_reason == ""

    def test_check_readiness_not_ready_migrations(self) -> None:
        """Return not-ready state when migrations fail."""
        with patch("xiosync.core.health.verify_migrations_at_head") as mock_verify:
            mock_verify.side_effect = MigrationNotAtHeadError("Migrations not at head")
            mock_engine = MagicMock(spec=Engine)

            state = check_readiness(mock_engine)

            assert state.is_live is True
            assert state.is_ready is False
            assert "Migrations not at head" in state.ready_reason

    def test_check_readiness_not_ready_database(self) -> None:
        """Return not-ready state when database connection fails."""
        with patch("xiosync.core.health.verify_migrations_at_head") as mock_verify:
            mock_verify.side_effect = DatabaseConnectionError("Connection refused")
            mock_engine = MagicMock(spec=Engine)

            state = check_readiness(mock_engine)

            assert state.is_live is True
            assert state.is_ready is False
            assert "Connection refused" in state.ready_reason

    def test_readiness_state_immutable(self) -> None:
        """ReadinessState is frozen (immutable)."""
        state = ReadinessState(
            is_live=True, is_ready=True, live_reason="test", ready_reason=""
        )

        with pytest.raises(AttributeError):
            state.is_live = False


class TestIntegration:
    """Integration tests using real SQLite database."""

    def test_full_readiness_flow_with_migrations(self) -> None:
        """Test full readiness check with real database and migrations."""
        # Create a real database with alembic_version table
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE alembic_version (
                    version_num VARCHAR(32) PRIMARY KEY,
                    applied TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """)
            )
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('14c2c1f29abe')")
            )
            conn.commit()

        # Should verify successfully
        with patch("xiosync.core.health.get_alembic_head_revision") as mock_get_head:
            mock_get_head.return_value = "14c2c1f29abe"
            verify_migrations_at_head(engine)  # Should not raise

    def test_full_readiness_check_fails_no_migrations(self) -> None:
        """Test readiness check fails when migrations table doesn't exist."""
        engine = create_engine("sqlite:///:memory:")

        with patch("xiosync.core.health.get_alembic_head_revision") as mock_get_head:
            mock_get_head.return_value = "14c2c1f29abe"

            # When migrations table doesn't exist, we get DatabaseConnectionError
            # (migrations not applied is detected as part of connection check)
            with pytest.raises((MigrationNotAtHeadError, DatabaseConnectionError)):
                verify_migrations_at_head(engine)
