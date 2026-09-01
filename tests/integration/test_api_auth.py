"""HTTP contracts for Phase 1 Step 7 authentication and middleware."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from xiosync.api.app import create_app
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.platform.clock import FixedClock
from xiosync.services.identity import AuthenticationError, TokenPair

NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
ORG_ID = uuid.uuid4()
IDENTITY_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()
SESSION_ID = uuid.uuid4()
CONTEXT = OrgContext(
    auth_identity_id=IDENTITY_ID,
    actor_id=ACTOR_ID,
    organization_id=ORG_ID,
    session_id=SESSION_ID,
    platform_role=PlatformRole.NONE,
    membership_role=MembershipRole.ORG_ADMIN,
)


class FakeSessionService:
    def __init__(self) -> None:
        self.valid_access_token = "access-token"
        self.current_refresh_token = "refresh-token"
        self.logged_out: OrgContext | None = None
        self.scoped = False

    def _pair(self) -> TokenPair:
        return TokenPair(
            access_token=self.valid_access_token,
            access_token_expires_at=NOW + timedelta(minutes=15),
            refresh_token=self.current_refresh_token,
            session_id=SESSION_ID,
            organization_id=ORG_ID,
        )

    def login(
        self, organization_id: uuid.UUID, email: str, password: str, *, now: datetime
    ) -> TokenPair:
        if (organization_id, email, password, now) != (
            ORG_ID,
            "owner@example.com",
            "correct-password",
            NOW,
        ):
            raise AuthenticationError
        return self._pair()

    def refresh(self, refresh_token: str, *, now: datetime) -> TokenPair:
        if refresh_token != self.current_refresh_token or now != NOW:
            raise AuthenticationError
        self.current_refresh_token = "rotated-refresh-token"
        return self._pair()

    def validate_access_token(self, access_token: str, *, now: datetime) -> OrgContext:
        if access_token != self.valid_access_token or now != NOW or self.logged_out is not None:
            raise AuthenticationError
        return CONTEXT

    def logout(self, context: OrgContext, *, now: datetime) -> None:
        assert self.scoped, "org_scoped_session must be active before the handler"
        assert now == NOW
        self.logged_out = context


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeSessionService]:
    service = FakeSessionService()

    @contextmanager
    def fake_org_scope(engine: Any, context: OrgContext) -> Any:
        del engine
        assert context == CONTEXT
        service.scoped = True
        try:
            yield object()
        finally:
            service.scoped = False

    monkeypatch.setattr("xiosync.api.middleware.org_scoped_session", fake_org_scope)
    app = create_app(
        session_service=service,  # type: ignore[arg-type]
        engine=object(),  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        max_body_bytes=256,
    )

    @app.get("/api/v1/context-check")
    def context_check(request: Request) -> dict[str, str | bool]:
        return {
            "organization_id": str(request.state.org_context.organization_id),
            "scoped": service.scoped,
        }

    return TestClient(app), service


def test_login_refresh_and_logout_contract(
    client: tuple[TestClient, FakeSessionService],
) -> None:
    http, service = client
    request_id = "contract-request-1"
    login = http.post(
        "/api/v1/auth/login",
        headers={"X-Request-ID": request_id},
        json={
            "organization_id": str(ORG_ID),
            "email": "owner@example.com",
            "password": "correct-password",
        },
    )
    assert login.status_code == 200
    assert login.json()["request_id"] == request_id
    assert login.json()["token_type"] == "bearer"
    assert login.headers["x-request-id"] == request_id
    assert login.headers["x-content-type-options"] == "nosniff"
    assert login.headers["x-frame-options"] == "DENY"
    assert login.headers["cache-control"] == "no-store"

    refresh = http.post(
        "/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert refresh.status_code == 200
    assert refresh.json()["refresh_token"] == "rotated-refresh-token"

    scoped = http.get("/api/v1/context-check", headers={"Authorization": "Bearer access-token"})
    assert scoped.json() == {"organization_id": str(ORG_ID), "scoped": True}

    logout = http.post("/api/v1/auth/logout", headers={"Authorization": "Bearer access-token"})
    assert logout.status_code == 200
    assert logout.json()["status"] == "logged_out"
    assert service.logged_out == CONTEXT

    revoked = http.get("/api/v1/context-check", headers={"Authorization": "Bearer access-token"})
    assert revoked.status_code == 401
    assert revoked.headers["content-type"].startswith("application/problem+json")
    assert revoked.json()["code"] == "authentication_failed"


def test_authentication_is_fail_closed_and_non_oracular(
    client: tuple[TestClient, FakeSessionService],
) -> None:
    http, _ = client
    missing = http.post("/api/v1/auth/logout")
    invalid = http.post("/api/v1/auth/logout", headers={"Authorization": "Bearer invalid"})
    assert missing.status_code == invalid.status_code == 401
    assert missing.json()["code"] == invalid.json()["code"] == "authentication_failed"
    assert missing.json()["title"] == invalid.json()["title"] == "Authentication failed"
    assert "x-request-id" in missing.headers


def test_invalid_login_and_oversized_body_are_typed_problems(
    client: tuple[TestClient, FakeSessionService],
) -> None:
    http, _ = client
    bad_login = http.post(
        "/api/v1/auth/login",
        json={
            "organization_id": str(ORG_ID),
            "email": "owner@example.com",
            "password": "wrong",
        },
    )
    assert bad_login.status_code == 401
    assert bad_login.headers["content-type"].startswith("application/problem+json")
    assert bad_login.json()["code"] == "authentication_failed"

    too_large = http.post(
        "/api/v1/auth/login",
        content=b"x" * 257,
        headers={"content-type": "application/json", "x-request-id": "large-request"},
    )
    assert too_large.status_code == 413
    assert too_large.json()["code"] == "body_too_large"
    assert too_large.json()["request_id"] == "large-request"
    assert too_large.headers["x-content-type-options"] == "nosniff"
