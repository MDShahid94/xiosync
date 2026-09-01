"""Tests for StrictCORSMiddleware and validate_origins (Phase 7 Step 3 / G-OPS-1).

Verifies:
- Allowlisted origin receives CORS headers.
- Non-allowlisted origin receives no CORS headers.
- Wildcard '*' is rejected at config time (validate_origins).
- Empty allowlist is rejected in staging/production.
- Empty allowlist is accepted in dev (default-permissive for local work).
- OPTIONS preflight for an allowlisted origin returns 200 with correct header.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from xiosync.api.middleware.cors import StrictCORSMiddleware, validate_origins
from xiosync.platform.config import ConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app_with_origins(allowed: list[str]) -> FastAPI:
    """Minimal app wrapped with StrictCORSMiddleware."""
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(StrictCORSMiddleware, allowed_origins=allowed)
    return app


# ---------------------------------------------------------------------------
# Unit tests — validate_origins
# ---------------------------------------------------------------------------


def test_wildcard_origin_rejected_at_config() -> None:
    """validate_origins must raise ConfigError if '*' appears (INV-CORS-1 / C4)."""
    with pytest.raises(ConfigError, match="Wildcard"):
        validate_origins(["*"], "dev")


def test_empty_origins_rejected_in_staging() -> None:
    """Empty allowlist in staging must raise ConfigError."""
    with pytest.raises(ConfigError, match="CORS_ALLOWED_ORIGINS"):
        validate_origins([], "staging")


def test_empty_origins_rejected_in_production() -> None:
    """Empty allowlist in production must raise ConfigError."""
    with pytest.raises(ConfigError, match="CORS_ALLOWED_ORIGINS"):
        validate_origins([], "production")


def test_empty_origins_allowed_in_dev() -> None:
    """Empty allowlist in dev is fine (local default-permissive)."""
    validate_origins([], "dev")  # must not raise


def test_empty_origins_allowed_in_ci() -> None:
    """Empty allowlist in ci is fine."""
    validate_origins([], "ci")  # must not raise


# ---------------------------------------------------------------------------
# Integration tests — StrictCORSMiddleware + TestClient
# ---------------------------------------------------------------------------


def test_allowed_origin_gets_cors_headers() -> None:
    """An allowlisted origin must receive the Access-Control-Allow-Origin header."""
    client = TestClient(_app_with_origins(["https://app.xiosync.dev"]))
    resp = client.get("/ping", headers={"Origin": "https://app.xiosync.dev"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://app.xiosync.dev"


def test_non_allowlisted_origin_rejected() -> None:
    """A non-allowlisted origin must NOT receive CORS headers (G-OPS-1)."""
    client = TestClient(_app_with_origins(["https://app.xiosync.dev"]))
    resp = client.get("/ping", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 200  # request succeeds, but no CORS headers
    assert "access-control-allow-origin" not in resp.headers


def test_options_preflight_allowed_origin() -> None:
    """OPTIONS preflight from an allowlisted origin must return 200 with CORS headers."""
    client = TestClient(
        _app_with_origins(["https://app.xiosync.dev"]),
        raise_server_exceptions=False,
    )
    resp = client.options(
        "/ping",
        headers={
            "Origin": "https://app.xiosync.dev",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://app.xiosync.dev"
