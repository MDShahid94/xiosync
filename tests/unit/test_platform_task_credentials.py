"""Unit tests for single-use, per-lease task credentials (doc 07 §3;
INV-TASK-SEC-1/2; D-007)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from xiosync.platform.task_credentials import (
    TASK_CREDENTIAL_MAX_TTL,
    TaskCredentialError,
    load_task_credential_signing_key,
    mint_task_credential,
    verify_task_credential,
)

SECRET = "unit-test-worker-credential-key-0123456789abcdef"
NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
EXPIRES = NOW + timedelta(minutes=5)

TASK_ID = uuid.uuid4()
WORKER_ID = uuid.uuid4()
LEASE_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
CAP_ID = uuid.uuid4()


def mint(
    *,
    now: datetime = NOW,
    expires_at: datetime = EXPIRES,
    scoped_capabilities: list[uuid.UUID] | None = None,
) -> tuple[str, object]:
    return mint_task_credential(
        SECRET,
        task_id=TASK_ID,
        worker_id=WORKER_ID,
        lease_id=LEASE_ID,
        organization_id=ORG_ID,
        scoped_capabilities=[CAP_ID] if scoped_capabilities is None else scoped_capabilities,
        now=now,
        expires_at=expires_at,
    )


def test_round_trip_preserves_all_claims() -> None:
    token, issued = mint()
    claims = verify_task_credential(SECRET, token, now=NOW)
    assert claims == issued
    assert claims.task_id == TASK_ID
    assert claims.worker_id == WORKER_ID
    assert claims.lease_id == LEASE_ID
    assert claims.organization_id == ORG_ID
    assert claims.scoped_capabilities == (CAP_ID,)
    assert claims.expires_at == EXPIRES


def test_each_credential_gets_a_unique_jti() -> None:
    _, first = mint()
    _, second = mint()
    assert first.jti != second.jti  # type: ignore[attr-defined]


def test_binding_matches_when_task_and_worker_supplied() -> None:
    token, _ = mint()
    claims = verify_task_credential(
        SECRET, token, now=NOW, expected_task_id=TASK_ID, expected_worker_id=WORKER_ID
    )
    assert claims.task_id == TASK_ID


def test_replay_on_another_task_is_rejected() -> None:
    """INV-TASK-SEC-2: a credential cannot be presented for a different task."""
    token, _ = mint()
    with pytest.raises(TaskCredentialError):
        verify_task_credential(SECRET, token, now=NOW, expected_task_id=uuid.uuid4())


def test_replay_by_another_worker_is_rejected() -> None:
    """INV-TASK-SEC-2: a credential is bound to the worker that leased it."""
    token, _ = mint()
    with pytest.raises(TaskCredentialError):
        verify_task_credential(SECRET, token, now=NOW, expected_worker_id=uuid.uuid4())


def test_expired_credential_is_rejected() -> None:
    """INV-TASK-SEC-2: the credential expires with the lease."""
    token, _ = mint(expires_at=NOW + timedelta(minutes=5))
    with pytest.raises(TaskCredentialError):
        verify_task_credential(SECRET, token, now=NOW + timedelta(minutes=5))


def test_wrong_secret_is_rejected() -> None:
    token, _ = mint()
    with pytest.raises(TaskCredentialError):
        verify_task_credential("other-secret-material-0123456789abcdef", token, now=NOW)


def test_user_jwt_secret_cannot_forge_a_task_credential() -> None:
    """H7 remediation: the signing key is distinct from the user JWT secret, so
    a token signed with the user secret is rejected."""
    token, _ = mint()
    user_jwt_secret = "user-session-jwt-secret-DIFFERENT-0123456789"
    with pytest.raises(TaskCredentialError):
        verify_task_credential(user_jwt_secret, token, now=NOW)


def test_garbage_token_is_rejected() -> None:
    with pytest.raises(TaskCredentialError):
        verify_task_credential(SECRET, "not.a.jwt", now=NOW)


def test_alg_none_is_rejected() -> None:
    payload = {
        "jti": "x",
        "tsk": str(TASK_ID),
        "wkr": str(WORKER_ID),
        "lse": str(LEASE_ID),
        "org": str(ORG_ID),
        "cap": [str(CAP_ID)],
        "iat": int(NOW.timestamp()),
        "exp": int(EXPIRES.timestamp()),
    }
    unsigned = pyjwt.encode(payload, None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(TaskCredentialError):
        verify_task_credential(SECRET, unsigned, now=NOW)


def test_missing_required_claim_is_rejected() -> None:
    payload = {
        "jti": "x",
        "tsk": str(TASK_ID),
        "wkr": str(WORKER_ID),
        # no "lse"
        "org": str(ORG_ID),
        "cap": [str(CAP_ID)],
        "iat": int(NOW.timestamp()),
        "exp": int(EXPIRES.timestamp()),
    }
    token = pyjwt.encode(payload, SECRET, algorithm="HS256")
    with pytest.raises(TaskCredentialError):
        verify_task_credential(SECRET, token, now=NOW)


def test_empty_scoped_capabilities_is_refused_at_mint() -> None:
    with pytest.raises(ValueError, match="scoped_capabilities"):
        mint(scoped_capabilities=[])


def test_ttl_above_ceiling_is_refused_at_mint() -> None:
    with pytest.raises(ValueError, match="TTL"):
        mint(expires_at=NOW + TASK_CREDENTIAL_MAX_TTL + timedelta(seconds=1))


def test_non_positive_ttl_is_refused_at_mint() -> None:
    with pytest.raises(ValueError, match="after now"):
        mint(expires_at=NOW)


def test_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        mint(now=datetime(2026, 7, 22))  # noqa: DTZ001
    token, _ = mint()
    with pytest.raises(ValueError, match="timezone-aware"):
        verify_task_credential(SECRET, token, now=datetime(2026, 7, 22))  # noqa: DTZ001


def test_empty_secret_is_refused_at_mint() -> None:
    with pytest.raises(ValueError, match="secret"):
        mint_task_credential(
            "",
            task_id=TASK_ID,
            worker_id=WORKER_ID,
            lease_id=LEASE_ID,
            organization_id=ORG_ID,
            scoped_capabilities=[CAP_ID],
            now=NOW,
            expires_at=EXPIRES,
        )


def test_load_signing_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_CREDENTIAL_KEY", SECRET)
    assert load_task_credential_signing_key() == SECRET


def test_load_signing_key_fails_loud_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-SEC-1: a missing key fails loudly, never a silent default."""
    monkeypatch.delenv("WORKER_CREDENTIAL_KEY", raising=False)
    with pytest.raises(RuntimeError, match="WORKER_CREDENTIAL_KEY"):
        load_task_credential_signing_key()
