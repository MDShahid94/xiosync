"""Unit tests for the fail-fast config loader (doc 09 §1, INV-CFG-1/2; C6)."""

from __future__ import annotations

import pytest
from xiosync.platform.config import Config, ConfigError, Environment, load_config

_TEST_AUTH_SECRET = "unit-test-signing-secret-0123456789abcdef"  # >=32 chars

VALID_ENV = {
    "XIOSYNC_ENVIRONMENT": "dev",
    "DATABASE_URL": "postgresql+psycopg://user:pw@host:5432/xiosync",
    "XIOSYNC_AUTH_SECRET": _TEST_AUTH_SECRET,
}


def test_valid_config_loads_with_defaulted_log_level() -> None:
    config = load_config(VALID_ENV)
    assert config == Config(
        environment=Environment.DEV,
        database_url="postgresql+psycopg://user:pw@host:5432/xiosync",
        log_level="INFO",
        auth_secret=_TEST_AUTH_SECRET,
    )


def test_all_environments_and_explicit_log_level() -> None:
    for name in ("dev", "ci", "staging", "production"):
        config = load_config(
            {
                **VALID_ENV,
                "XIOSYNC_ENVIRONMENT": name,
                "XIOSYNC_LOG_LEVEL": "debug",
                # Phase 7 Step 3 (INV-CORS-1) requires an explicit allowlist in
                # staging and production.  Providing it for all environments
                # keeps this test focused on log-level/environment parsing.
                "CORS_ALLOWED_ORIGINS": "https://app.example.com",
            }
        )
        assert config.environment.value == name
        assert config.log_level == "DEBUG"


def test_plain_postgresql_scheme_is_accepted() -> None:
    config = load_config({**VALID_ENV, "DATABASE_URL": "postgresql://u:p@h/db"})
    assert config.database_url == "postgresql://u:p@h/db"


def test_short_auth_secret_fails() -> None:
    with pytest.raises(ConfigError, match="XIOSYNC_AUTH_SECRET"):
        load_config({**VALID_ENV, "XIOSYNC_AUTH_SECRET": "too-short"})


@pytest.mark.parametrize(
    "missing_key", ["XIOSYNC_ENVIRONMENT", "DATABASE_URL", "XIOSYNC_AUTH_SECRET"]
)
def test_missing_required_key_fails(missing_key: str) -> None:
    env = {key: value for key, value in VALID_ENV.items() if key != missing_key}
    with pytest.raises(ConfigError, match=missing_key):
        load_config(env)


@pytest.mark.parametrize("empty_value", ["", "   "])
def test_empty_required_value_fails(empty_value: str) -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_config({**VALID_ENV, "DATABASE_URL": empty_value})


@pytest.mark.parametrize(
    "bad_url",
    [
        "sqlite:///memory.db",
        "mysql://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",
        "not-a-url",
    ],
)
def test_non_postgres_database_url_fails(bad_url: str) -> None:
    with pytest.raises(ConfigError):
        load_config({**VALID_ENV, "DATABASE_URL": bad_url})


def test_unknown_environment_name_fails() -> None:
    with pytest.raises(ConfigError, match="XIOSYNC_ENVIRONMENT"):
        load_config({**VALID_ENV, "XIOSYNC_ENVIRONMENT": "prod"})


def test_unknown_prefixed_key_fails(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ConfigError, match="XIOSYNC_DATABSE_URL"):
        load_config({**VALID_ENV, "XIOSYNC_DATABSE_URL": "typo"})


def test_invalid_log_level_fails() -> None:
    with pytest.raises(ConfigError, match="XIOSYNC_LOG_LEVEL"):
        load_config({**VALID_ENV, "XIOSYNC_LOG_LEVEL": "verbose"})


def test_config_is_immutable() -> None:
    config = load_config(VALID_ENV)
    with pytest.raises(AttributeError):
        config.log_level = "DEBUG"  # type: ignore[misc]
