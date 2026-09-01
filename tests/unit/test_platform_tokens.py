"""Unit tests for access-token issue/verify (doc 05 §2.2; D-023)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from xiosync.platform.tokens import (
    ACCESS_TOKEN_MAX_TTL,
    TokenError,
    issue_access_token,
    verify_access_token,
)

SECRET = "unit-test-signing-secret-0123456789abcdef"
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

SESSION_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()


def issue(now: datetime = NOW, ttl: timedelta = ACCESS_TOKEN_MAX_TTL) -> tuple[str, object]:
    return issue_access_token(
        SECRET,
        session_id=SESSION_ID,
        organization_id=ORG_ID,
        actor_id=ACTOR_ID,
        now=now,
        ttl=ttl,
    )


def test_round_trip_preserves_all_claims() -> None:
    token, issued = issue()
    claims = verify_access_token(SECRET, token, now=NOW)
    assert claims == issued
    assert claims.session_id == SESSION_ID
    assert claims.organization_id == ORG_ID
    assert claims.actor_id == ACTOR_ID
    assert claims.expires_at == NOW + ACCESS_TOKEN_MAX_TTL


def test_each_token_gets_a_unique_jti() -> None:
    _, first = issue()
    _, second = issue()
    assert first.jti != second.jti  # type: ignore[attr-defined]


def test_expired_token_is_rejected() -> None:
    token, _ = issue(ttl=timedelta(minutes=5))
    with pytest.raises(TokenError):
        verify_access_token(SECRET, token, now=NOW + timedelta(minutes=5))


def test_wrong_secret_is_rejected() -> None:
    token, _ = issue()
    with pytest.raises(TokenError):
        verify_access_token("other-secret-material-0123456789abcdef", token, now=NOW)


def test_garbage_token_is_rejected() -> None:
    with pytest.raises(TokenError):
        verify_access_token(SECRET, "not.a.jwt", now=NOW)


def test_alg_none_is_rejected() -> None:
    payload = {
        "jti": "x",
        "sid": str(SESSION_ID),
        "org": str(ORG_ID),
        "act": str(ACTOR_ID),
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
    }
    unsigned = pyjwt.encode(payload, None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(TokenError):
        verify_access_token(SECRET, unsigned, now=NOW)


def test_missing_required_claim_is_rejected() -> None:
    payload = {
        "sid": str(SESSION_ID),
        "org": str(ORG_ID),
        "act": str(ACTOR_ID),
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
    }  # no jti
    token = pyjwt.encode(payload, SECRET, algorithm="HS256")
    with pytest.raises(TokenError):
        verify_access_token(SECRET, token, now=NOW)


def test_ttl_above_fifteen_minutes_is_refused_at_issue() -> None:
    with pytest.raises(ValueError, match="ttl"):
        issue(ttl=timedelta(minutes=16))


def test_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        issue(now=datetime(2026, 7, 15))  # noqa: DTZ001
    token, _ = issue()
    with pytest.raises(ValueError, match="timezone-aware"):
        verify_access_token(SECRET, token, now=datetime(2026, 7, 15))  # noqa: DTZ001


def test_empty_secret_is_refused_at_issue() -> None:
    with pytest.raises(ValueError, match="secret"):
        issue_access_token(
            "",
            session_id=SESSION_ID,
            organization_id=ORG_ID,
            actor_id=ACTOR_ID,
            now=NOW,
        )
