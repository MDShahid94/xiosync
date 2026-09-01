"""XIOSYNC load test suite using Locust.

Usage::

    # Install: pip install locust
    # Run:     locust -f tools/load_test.py --host http://localhost:8000

    # Headless mode (CI):
    locust -f tools/load_test.py --host http://localhost:8000 \
           --headless -u 50 -r 5 --run-time 60s \
           --csv results/load_test

Scenarios:
    - HealthUser: Hammers /live and /ready (baseline throughput)
    - AuthUser: Login → refresh → logout cycle
    - APIUser: Full workflow: create workflow → start run → list tasks → events
"""

from __future__ import annotations

import json
import random
import string
import uuid

from locust import HttpUser, between, task, tag


class HealthUser(HttpUser):
    """Baseline health probe load — should handle thousands of RPS."""

    weight = 3
    wait_time = between(0.1, 0.5)

    @task(3)
    @tag("health")
    def liveness(self) -> None:
        self.client.get("/live", name="/live")

    @task(1)
    @tag("health")
    def readiness(self) -> None:
        self.client.get("/ready", name="/ready")


class AuthUser(HttpUser):
    """Authentication flow load test."""

    weight = 2
    wait_time = between(1, 3)
    token: str = ""

    def on_start(self) -> None:
        """Login to get a token."""
        resp = self.client.post(
            "/api/v1/auth/login",
            json={
                "email": f"loadtest-{uuid.uuid4().hex[:8]}@example.com",
                "password": "LoadTest!2024",
            },
            name="/api/v1/auth/login",
        )
        if resp.status_code == 200:
            data = resp.json()
            self.token = data.get("access_token", "")

    @task(3)
    @tag("auth")
    def refresh_token(self) -> None:
        if self.token:
            self.client.post(
                "/api/v1/auth/refresh",
                headers={"Authorization": f"Bearer {self.token}"},
                name="/api/v1/auth/refresh",
            )

    @task(1)
    @tag("auth")
    def logout(self) -> None:
        if self.token:
            self.client.post(
                "/api/v1/auth/logout",
                headers={"Authorization": f"Bearer {self.token}"},
                name="/api/v1/auth/logout",
            )
            self.token = ""


class APIUser(HttpUser):
    """Authenticated API workflow load test."""

    weight = 5
    wait_time = between(1, 5)
    token: str = ""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def on_start(self) -> None:
        resp = self.client.post(
            "/api/v1/auth/login",
            json={
                "email": f"apitest-{uuid.uuid4().hex[:8]}@example.com",
                "password": "LoadTest!2024",
            },
            name="/api/v1/auth/login",
        )
        if resp.status_code == 200:
            self.token = resp.json().get("access_token", "")

    @task(5)
    @tag("listings")
    def list_workflows(self) -> None:
        self.client.get(
            "/api/v1/workflows?limit=20",
            headers=self._headers(),
            name="/api/v1/workflows",
        )

    @task(3)
    @tag("listings")
    def list_tasks(self) -> None:
        self.client.get(
            "/api/v1/tasks?limit=20",
            headers=self._headers(),
            name="/api/v1/tasks",
        )

    @task(3)
    @tag("listings")
    def list_events(self) -> None:
        self.client.get(
            "/api/v1/events?limit=20",
            headers=self._headers(),
            name="/api/v1/events",
        )

    @task(2)
    @tag("listings")
    def list_workers(self) -> None:
        self.client.get(
            "/api/v1/workers?limit=20",
            headers=self._headers(),
            name="/api/v1/workers",
        )

    @task(1)
    @tag("metering")
    def metering_summary(self) -> None:
        self.client.get(
            "/api/v1/metering/summary",
            headers=self._headers(),
            name="/api/v1/metering/summary",
        )

    @task(1)
    @tag("triggers")
    def list_triggers(self) -> None:
        self.client.get(
            "/api/v1/triggers",
            headers=self._headers(),
            name="/api/v1/triggers",
        )

    @task(1)
    @tag("openapi")
    def openapi_spec(self) -> None:
        self.client.get("/openapi.json", name="/openapi.json")
