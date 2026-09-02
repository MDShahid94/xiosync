"""Fail-fast configuration schema and loader.

Normative sources: doc 09 §1 (INV-CFG-1/2/3), doc 09 §4 (INV-STARTUP-1/3),
doc 04 §5 (PostgreSQL-only — C6), L4 (no embedded secret defaults).

Rules enforced here:
- Every required key must be present and non-empty; a missing required key
  raises ``ConfigError`` (the process must then exit non-zero — no broad
  ``except Exception`` may mask it).
- There are no embedded defaults for secrets or environment-specific values.
  The only defaulted key is the non-secret ``XIOSYNC_LOG_LEVEL``.
- Unknown ``XIOSYNC_``-prefixed keys fail fast (INV-CFG-2): a typo in an
  operator-supplied key must never be silently ignored.
- ``DATABASE_URL`` must target PostgreSQL via psycopg. SQLite or any other
  engine is rejected outright (C6, D-003).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

ENV_PREFIX = "XIOSYNC_"

_KEY_ENVIRONMENT = "XIOSYNC_ENVIRONMENT"
_KEY_LOG_LEVEL = "XIOSYNC_LOG_LEVEL"
_KEY_DATABASE_URL = "DATABASE_URL"
_KEY_AUTH_SECRET = "XIOSYNC_AUTH_SECRET"  # noqa: S105 — env-var *name*, not a secret
_KEY_REDIS_URL = "REDIS_URL"
_KEY_RATE_LIMIT_AUTH = "RATE_LIMIT_AUTH_LIMIT"
_KEY_RATE_LIMIT_API = "RATE_LIMIT_API_LIMIT"
_KEY_RATE_LIMIT_WINDOW = "RATE_LIMIT_WINDOW_SECONDS"
_KEY_CORS_ALLOWED_ORIGINS = "CORS_ALLOWED_ORIGINS"

# Signing secrets shorter than this are guessable; 32 bytes of entropy
# rendered as text is the operational floor (doc 05 §2.2; L4 — no default).
_MIN_AUTH_SECRET_LENGTH = 32

_KNOWN_PREFIXED_KEYS = frozenset({
    _KEY_ENVIRONMENT,
    _KEY_LOG_LEVEL,
    _KEY_AUTH_SECRET,
    # Wave 3 — P-4 API governance
    "XIOSYNC_API_DEPRECATION_CONFIG",
    # Wave 3 — M-2 cross-org sharing
    "XIOSYNC_ENABLE_CROSS_ORG_SHARING",
    # Genesis Phase 0 — bootstrap token for API-based genesis
    "XIOSYNC_BOOTSTRAP_TOKEN",
})

_ALLOWED_DATABASE_SCHEMES = frozenset({"postgresql", "postgresql+psycopg"})

_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_DEFAULT_LOG_LEVEL = "INFO"


class ConfigError(ValueError):
    """A configuration violation. Startup must abort (INV-STARTUP-3)."""


class Environment(StrEnum):
    """The four explicit environments of doc 09 §1."""

    DEV = "dev"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class Config:
    """Validated process configuration. Immutable after load."""

    environment: Environment
    database_url: str
    log_level: str
    auth_secret: str
    redis_url: str | None = None
    rate_limit_auth_limit: int = 20
    rate_limit_api_limit: int = 100
    rate_limit_window_seconds: int = 60
    cors_allowed_origins: list[str] = field(default_factory=list)


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"required configuration key {key!r} is missing or empty")
    return value


def _validate_database_url(url: str) -> str:
    scheme, separator, _ = url.partition("://")
    if not separator or scheme not in _ALLOWED_DATABASE_SCHEMES:
        allowed = ", ".join(sorted(_ALLOWED_DATABASE_SCHEMES))
        raise ConfigError(
            f"{_KEY_DATABASE_URL} must use one of the schemes [{allowed}]; "
            f"got scheme {scheme!r} (PostgreSQL is the only supported engine — C6)"
        )
    return url


def _reject_unknown_keys(env: Mapping[str, str]) -> None:
    unknown = sorted(
        key for key in env if key.startswith(ENV_PREFIX) and key not in _KNOWN_PREFIXED_KEYS
    )
    if unknown:
        raise ConfigError(f"unknown configuration keys: {', '.join(unknown)}")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Load and validate configuration from ``env`` (default: ``os.environ``).

    Raises ``ConfigError`` on the first violation. Callers must not catch it
    to continue booting (INV-STARTUP-3).
    """
    source: Mapping[str, str] = os.environ if env is None else env

    _reject_unknown_keys(source)

    raw_environment = _require(source, _KEY_ENVIRONMENT)
    try:
        environment = Environment(raw_environment)
    except ValueError:
        allowed = ", ".join(member.value for member in Environment)
        raise ConfigError(
            f"{_KEY_ENVIRONMENT} must be one of [{allowed}]; got {raw_environment!r}"
        ) from None

    database_url = _validate_database_url(_require(source, _KEY_DATABASE_URL))

    auth_secret = _require(source, _KEY_AUTH_SECRET)
    if len(auth_secret) < _MIN_AUTH_SECRET_LENGTH:
        raise ConfigError(
            f"{_KEY_AUTH_SECRET} must be at least {_MIN_AUTH_SECRET_LENGTH} "
            "characters (doc 05 §2.2); generate one with e.g. "
            "`openssl rand -base64 48`"
        )

    log_level = source.get(_KEY_LOG_LEVEL, _DEFAULT_LOG_LEVEL).strip().upper()
    if log_level not in _ALLOWED_LOG_LEVELS:
        allowed = ", ".join(sorted(_ALLOWED_LOG_LEVELS))
        raise ConfigError(f"{_KEY_LOG_LEVEL} must be one of [{allowed}]; got {log_level!r}")

    def positive_int(key: str, default: int) -> int:
        raw = source.get(key, str(default)).strip()
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"{key} must be an integer") from None
        if value <= 0:
            raise ConfigError(f"{key} must be positive")
        return value

    redis_url = source.get(_KEY_REDIS_URL, "").strip() or None
    rate_limit_auth_limit = positive_int(_KEY_RATE_LIMIT_AUTH, 20)
    rate_limit_api_limit = positive_int(_KEY_RATE_LIMIT_API, 100)
    rate_limit_window_seconds = positive_int(_KEY_RATE_LIMIT_WINDOW, 60)

    raw_cors = source.get(_KEY_CORS_ALLOWED_ORIGINS, "").strip()
    cors_allowed_origins = [o.strip() for o in raw_cors.split(",") if o.strip()] if raw_cors else []
    if "*" in cors_allowed_origins:
        raise ConfigError(
            "Wildcard '*' is forbidden in CORS_ALLOWED_ORIGINS (INV-CORS-1 / C4)."
        )
    if environment in (Environment.STAGING, Environment.PRODUCTION) and not cors_allowed_origins:
        raise ConfigError(
            f"CORS_ALLOWED_ORIGINS must be set in the {environment.value!r} environment "
            "(INV-CORS-1)."
        )

    return Config(
        environment=environment,
        database_url=database_url,
        log_level=log_level,
        auth_secret=auth_secret,
        redis_url=redis_url,
        rate_limit_auth_limit=rate_limit_auth_limit,
        rate_limit_api_limit=rate_limit_api_limit,
        rate_limit_window_seconds=rate_limit_window_seconds,
        cors_allowed_origins=cors_allowed_origins,
    )
