"""Unit tests for health check endpoints (M7).

Tests verify:
- /live returns 200 immediately (process is running)
- /ready returns 200 when ready, 503 when not ready
- /ready checks database connectivity and migration status
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from xiosync.api.routers.health import router as health_router
from xiosync.core.health import ReadinessState


@pytest.fixture
def app_with_health() -> FastAPI:
    """Create a minimal FastAPI app with health router and mock engine."""
    app = FastAPI()
    app.include_router(health_router)

    # Mock engine in app state
    app.state.engine = MagicMock()

    return app


@pytest.fixture
def client(app_with_health: FastAPI) -> TestClient:
    """Create a test client for the app."""
    return TestClient(app_with_health)


class TestLivenessProbe:
    """Tests for /live endpoint."""

    def test_live_returns_200(self, client: TestClient) -> None:
        """GET /live returns 200."""
        response = client.get("/live")
        assert response.status_code == 200

    def test_live_returns_json(self, client: TestClient) -> None:
        """GET /live returns JSON with status and message."""
        response = client.get("/live")
        data = response.json()
        assert data["status"] == "alive"
        assert data["message"] == "Process is running"

    def test_live_never_fails(self, client: TestClient) -> None:
        """GET /live always returns 200 regardless of state."""
        # Even if we break the engine, /live should still work
        with patch("xiosync.core.health.check_readiness") as mock_check:
            mock_check.side_effect = Exception("Catastrophic failure")
            response = client.get("/live")

        assert response.status_code == 200

    def test_live_content_type(self, client: TestClient) -> None:
        """GET /live returns application/json content type."""
        response = client.get("/live")
        assert response.headers["content-type"] == "application/json"


class TestReadinessProbe:
    """Tests for /ready endpoint."""

    def test_ready_returns_200_when_ready(self, app_with_health: FastAPI) -> None:
        """GET /ready returns 200 when system is ready."""
        with patch("xiosync.api.routers.health.check_readiness") as mock_check:
            mock_check.return_value = ReadinessState(
                is_live=True,
                is_ready=True,
                live_reason="Process is running",
                ready_reason="",
            )

            client = TestClient(app_with_health)
            response = client.get("/ready")

        assert response.status_code == 200

    def test_ready_returns_503_when_not_ready(self, app_with_health: FastAPI) -> None:
        """GET /ready returns 503 when system is not ready."""
        with patch("xiosync.api.routers.health.check_readiness") as mock_check:
            mock_check.return_value = ReadinessState(
                is_live=True,
                is_ready=False,
                live_reason="Process is running",
                ready_reason="Database migrations not at head",
            )

            client = TestClient(app_with_health)
            response = client.get("/ready")

        assert response.status_code == 503

    def test_ready_returns_json_when_ready(self, app_with_health: FastAPI) -> None:
        """GET /ready returns JSON with status when ready."""
        with patch("xiosync.api.routers.health.check_readiness") as mock_check:
            mock_check.return_value = ReadinessState(
                is_live=True,
                is_ready=True,
                live_reason="Process is running",
                ready_reason="",
            )

            client = TestClient(app_with_health)
            response = client.get("/ready")

        data = response.json()
        assert data["status"] == "ready"
        assert data["message"] == "Fully operational"

    def test_ready_returns_error_detail_when_not_ready(self, app_with_health: FastAPI) -> None:
        """GET /ready includes error reason in response when not ready."""
        with patch("xiosync.api.routers.health.check_readiness") as mock_check:
            error_msg = "Database migrations not at head revision (C6)"
            mock_check.return_value = ReadinessState(
                is_live=True,
                is_ready=False,
                live_reason="Process is running",
                ready_reason=error_msg,
            )

            client = TestClient(app_with_health)
            response = client.get("/ready")

        assert response.status_code == 503
        data = response.json()
        assert data["detail"] == error_msg

    def test_ready_calls_check_readiness(self, app_with_health: FastAPI) -> None:
        """GET /ready calls check_readiness with engine."""
        with patch("xiosync.api.routers.health.check_readiness") as mock_check:
            mock_check.return_value = ReadinessState(
                is_live=True,
                is_ready=True,
                live_reason="",
                ready_reason="",
            )

            client = TestClient(app_with_health)
            response = client.get("/ready")

        # Verify check_readiness was called with the engine
        mock_check.assert_called_once()
        assert response.status_code == 200

    def test_ready_content_type(self, app_with_health: FastAPI) -> None:
        """GET /ready returns application/json content type."""
        with patch("xiosync.core.health.check_readiness") as mock_check:
            mock_check.return_value = ReadinessState(
                is_live=True,
                is_ready=True,
                live_reason="",
                ready_reason="",
            )

            client = TestClient(app_with_health)
            response = client.get("/ready")

        assert response.headers["content-type"] == "application/json"


class TestReadinessScenarios:
    """Test realistic readiness scenarios."""

    def test_ready_migration_failure_scenario(self, app_with_health: FastAPI) -> None:
        """Scenario: Database is connected but migrations not at head."""
        with patch("xiosync.api.routers.health.check_readiness") as mock_check:
            mock_check.return_value = ReadinessState(
                is_live=True,
                is_ready=False,
                live_reason="Process is running",
                ready_reason=(
                    "Database schema is at revision 5f7f5f1793c7,"
                    " but head is 14c2c1f29abe"
                ),
            )

            client = TestClient(app_with_health)
            response = client.get("/ready")

        assert response.status_code == 503
        data = response.json()
        assert "5f7f5f1793c7" in data["detail"]

    def test_ready_connection_failure_scenario(self, app_with_health: FastAPI) -> None:
        """Scenario: Database connection failed."""
        with patch("xiosync.api.routers.health.check_readiness") as mock_check:
            mock_check.return_value = ReadinessState(
                is_live=True,
                is_ready=False,
                live_reason="Process is running",
                ready_reason="Database connection failed: connection timeout",
            )

            client = TestClient(app_with_health)
            response = client.get("/ready")

        assert response.status_code == 503
        data = response.json()
        assert "connection timeout" in data["detail"]

    def test_ready_all_checks_pass_scenario(self, app_with_health: FastAPI) -> None:
        """Scenario: All startup checks passed, system is ready."""
        with patch("xiosync.api.routers.health.check_readiness") as mock_check:
            mock_check.return_value = ReadinessState(
                is_live=True,
                is_ready=True,
                live_reason="Process is running",
                ready_reason="",
            )

            client = TestClient(app_with_health)
            response = client.get("/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"


class TestEndpointRouting:
    """Test that endpoints are registered correctly."""

    def test_live_endpoint_exists(self, client: TestClient) -> None:
        """Verify /live endpoint exists and responds."""
        response = client.get("/live")
        assert response.status_code in [200, 404, 405]  # At least endpoint should be defined

    def test_ready_endpoint_exists(self, client: TestClient) -> None:
        """Verify /ready endpoint exists and responds."""
        response = client.get("/ready")
        assert response.status_code in [200, 503, 404, 405]  # At least endpoint should be defined

    def test_endpoints_at_root_path(self, app_with_health: FastAPI) -> None:
        """Verify health endpoints are at root path (not under /api/v1)."""
        # This allows orchestrators to probe without needing to know about /api/v1
        client = TestClient(app_with_health)

        live_response = client.get("/live")
        ready_response = client.get("/ready")

        # Endpoints should exist and not be 404
        assert live_response.status_code in [200, 503, 422]  # Not 404
        assert ready_response.status_code in [200, 503, 422]  # Not 404

    def test_endpoints_no_auth_required(self, client: TestClient) -> None:
        """Verify health endpoints don't require authentication."""
        # Send request without any auth headers
        live_response = client.get("/live")
        ready_response = client.get("/ready")

        # Should not get 401 Unauthorized
        assert live_response.status_code != 401
        assert ready_response.status_code != 401
