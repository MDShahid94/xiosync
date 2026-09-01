"""Integration tests for app startup fail-fast validation (M5).

Tests verify:
- Configuration errors fail fast at startup
- Database connection errors fail fast
- Migration verification fails fast if not at head
- All checks must pass before app opens ports
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from xiosync.api.app import create_production_app
from xiosync.core.health import MigrationNotAtHeadError
from xiosync.platform.config import ConfigError


class TestStartupFailFast:
    """Tests for fail-fast startup (M5, INV-STARTUP-1)."""

    def test_startup_fails_on_missing_config(self) -> None:
        """Startup fails fast when required config is missing."""
        with patch("xiosync.api.app.load_config") as mock_load:
            mock_load.side_effect = ConfigError("XIOSYNC_ENVIRONMENT is required")

            with pytest.raises(ConfigError, match="XIOSYNC_ENVIRONMENT is required"):
                create_production_app()

    def test_startup_fails_on_invalid_database_url(self) -> None:
        """Startup fails fast when database URL is invalid."""
        with patch("xiosync.api.app.load_config") as mock_load:
            with patch("xiosync.api.app.create_database_engine") as mock_engine:
                with patch("xiosync.api.app.configure_logging"):
                    mock_config = MagicMock()
                    mock_config.environment = "production"
                    mock_config.log_level = "INFO"
                    mock_load.return_value = mock_config
                    mock_engine.side_effect = RuntimeError("Unsupported database engine")

                    with pytest.raises(RuntimeError, match="Unsupported database engine"):
                        create_production_app()

    def test_startup_fails_on_migration_not_at_head(self) -> None:
        """Startup fails fast when migrations are not at head (C6)."""
        with patch("xiosync.api.app.load_config") as mock_load:
            with patch("xiosync.api.app.create_database_engine") as mock_engine:
                with patch("xiosync.api.app.verify_migrations_at_head") as mock_verify:
                    mock_config = MagicMock()
                    mock_config.environment = "production"
                    mock_config.log_level = "INFO"
                    mock_config.database_url = "postgresql://localhost/test"
                    mock_config.auth_secret = "a" * 32
                    mock_load.return_value = mock_config

                    mock_engine_instance = MagicMock()
                    mock_engine.return_value = mock_engine_instance

                    error_msg = "Database schema at revision 5f7f5f1793c7, head is 14c2c1f29abe"
                    mock_verify.side_effect = MigrationNotAtHeadError(error_msg)

                    with pytest.raises(MigrationNotAtHeadError, match=error_msg):
                        create_production_app()

    def test_startup_succeeds_when_all_checks_pass(self) -> None:
        """Startup succeeds when all checks pass."""
        with patch("xiosync.api.app.load_config") as mock_load:
            with patch("xiosync.api.app.create_database_engine") as mock_engine:
                with patch("xiosync.api.app.verify_migrations_at_head") as mock_verify:
                    with patch("xiosync.api.app.configure_logging") as mock_logging:
                        with patch("xiosync.api.app.SessionService"):
                            with patch("xiosync.api.app.IdentityRepository"):
                                with patch("xiosync.api.app.SystemClock"):
                                    mock_config = MagicMock()
                                    mock_config.environment = "production"
                                    mock_config.log_level = "INFO"
                                    mock_config.database_url = "postgresql://localhost/test"
                                    mock_config.auth_secret = "a" * 32
                                    # Explicitly disable Redis so create_rate_limiter is
                                    # not called — this unit test is about startup order,
                                    # not Redis connectivity.
                                    mock_config.redis_url = None
                                    mock_config.cors_allowed_origins = []
                                    mock_load.return_value = mock_config

                                    mock_engine_instance = MagicMock()
                                    mock_engine.return_value = mock_engine_instance

                                    # All checks pass
                                    mock_verify.return_value = None  # No exception

                                    app = create_production_app()

                                    assert app is not None
                                    # Verify all checks were called
                                    mock_load.assert_called_once()
                                    mock_logging.assert_called_once_with("INFO")
                                    mock_engine.assert_called_once()
                                    mock_verify.assert_called_once()


class TestStartupOrderOfOperations:
    """Test that startup validation happens in the correct order (M5)."""

    def test_config_validated_before_db_connection(self) -> None:
        """Configuration is validated before attempting database connection."""
        call_order = []

        def mock_load():
            call_order.append("config_load")
            raise ConfigError("Config error")

        def mock_create_engine(url):
            call_order.append("db_engine_create")
            raise RuntimeError("Should not reach here")

        with patch("xiosync.api.app.load_config", side_effect=mock_load):
            with patch("xiosync.api.app.create_database_engine", side_effect=mock_create_engine):
                with pytest.raises(ConfigError):
                    create_production_app()

        assert call_order == ["config_load"]
        assert "db_engine_create" not in call_order

    def test_db_verified_before_migration_check(self) -> None:
        """Database connection is verified before migration check."""
        call_order = []

        def mock_load():
            call_order.append("config_load")
            config = MagicMock()
            config.environment = "production"
            config.log_level = "INFO"
            config.database_url = "postgresql://localhost/test"
            config.auth_secret = "a" * 32
            return config

        def mock_create_engine(url):
            call_order.append("db_engine_create")
            return MagicMock()

        def mock_verify(engine):
            call_order.append("migration_verify")
            raise MigrationNotAtHeadError("Not at head")

        with patch("xiosync.api.app.load_config", side_effect=mock_load):
            with patch("xiosync.api.app.create_database_engine", side_effect=mock_create_engine):
                with patch("xiosync.api.app.verify_migrations_at_head", side_effect=mock_verify):
                    with patch("xiosync.api.app.configure_logging"):
                        with pytest.raises(MigrationNotAtHeadError):
                            create_production_app()

        assert call_order == ["config_load", "db_engine_create", "migration_verify"]


class TestStartupLogging:
    """Test that startup produces helpful log messages (M5)."""

    def test_startup_logs_environment(self) -> None:
        """Startup logs the environment being started."""
        with patch("xiosync.api.app.load_config") as mock_load:
            with patch("xiosync.api.app.create_database_engine") as mock_engine:
                with patch("xiosync.api.app.verify_migrations_at_head") as mock_verify:
                    with patch("xiosync.api.app.configure_logging"):
                        with patch("xiosync.api.app.SessionService"):
                            with patch("xiosync.api.app.IdentityRepository"):
                                with patch("xiosync.api.app.SystemClock"):
                                    with patch("xiosync.api.app.logger") as mock_logger:
                                        mock_config = MagicMock()
                                        mock_config.environment = "staging"
                                        mock_config.log_level = "DEBUG"
                                        mock_config.database_url = "postgresql://localhost/test"
                                        mock_config.auth_secret = "a" * 32
                                        mock_config.redis_url = None
                                        mock_config.cors_allowed_origins = []
                                        mock_load.return_value = mock_config

                                        mock_engine.return_value = MagicMock()
                                        mock_verify.return_value = None

                                        create_production_app()

                                        # Should log environment
                                        calls = [str(call) for call in mock_logger.info.call_args_list]  # noqa: E501
                                        assert any("staging" in call for call in calls)


    def test_startup_logs_success(self) -> None:
        """Startup logs success message when all checks pass."""
        with patch("xiosync.api.app.load_config") as mock_load:
            with patch("xiosync.api.app.create_database_engine") as mock_engine:
                with patch("xiosync.api.app.verify_migrations_at_head") as mock_verify:
                    with patch("xiosync.api.app.configure_logging"):
                        with patch("xiosync.api.app.SessionService"):
                            with patch("xiosync.api.app.IdentityRepository"):
                                with patch("xiosync.api.app.SystemClock"):
                                    with patch("xiosync.api.app.logger") as mock_logger:
                                        mock_config = MagicMock()
                                        mock_config.environment = "production"
                                        mock_config.log_level = "INFO"
                                        mock_config.database_url = "postgresql://localhost/test"
                                        mock_config.auth_secret = "a" * 32
                                        mock_config.redis_url = None
                                        mock_config.cors_allowed_origins = []
                                        mock_load.return_value = mock_config

                                        mock_engine.return_value = MagicMock()
                                        mock_verify.return_value = None

                                        create_production_app()

                                        # Should log success
                                        calls = [str(call) for call in mock_logger.info.call_args_list]  # noqa: E501
                                        assert any("startup checks passed" in call for call in calls)  # noqa: E501

    def test_startup_logs_failure_reasons(self) -> None:
        """Startup logs helpful error messages on failure."""
        with patch("xiosync.api.app.load_config") as mock_load:
            with patch("xiosync.api.app.create_database_engine") as mock_engine:
                with patch("xiosync.api.app.verify_migrations_at_head") as mock_verify:
                    with patch("xiosync.api.app.configure_logging"):
                        with patch("xiosync.api.app.logger") as mock_logger:
                            mock_config = MagicMock()
                            mock_config.environment = "production"
                            mock_config.log_level = "INFO"
                            mock_config.database_url = "postgresql://localhost/test"
                            mock_config.auth_secret = "a" * 32
                            mock_load.return_value = mock_config

                            mock_engine.return_value = MagicMock()
                            error_msg = "Migrations not at head"
                            mock_verify.side_effect = MigrationNotAtHeadError(error_msg)

                            with pytest.raises(MigrationNotAtHeadError):
                                create_production_app()

                            # Should log the critical error
                            calls = [str(call) for call in mock_logger.critical.call_args_list]
                            assert any("Migration verification failed" in call for call in calls)
