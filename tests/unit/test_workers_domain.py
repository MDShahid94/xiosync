"""Unit tests for xiosync/domain/workers.py — pure predicate coverage.

No database, no fixtures, no I/O.  Every predicate in the module is exercised
here; the tests are named after the invariants they protect so the failure
message is self-documenting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from xiosync.domain.workers import (
    credential_is_expired,
    credential_is_valid,
    trust_tier_index,
    trust_tier_satisfies,
    worker_can_demote,
    worker_can_promote,
    worker_is_active,
    worker_is_enrollable,
)

# ---------------------------------------------------------------------------
# trust_tier_index
# ---------------------------------------------------------------------------


def test_trust_tier_index_known() -> None:
    """newcomer maps to 0 and admin maps to 4 (the ceiling)."""
    assert trust_tier_index("newcomer") == 0
    assert trust_tier_index("contributor") == 1
    assert trust_tier_index("trusted") == 2
    assert trust_tier_index("core") == 3
    assert trust_tier_index("admin") == 4


def test_trust_tier_index_unknown() -> None:
    """An unrecognised tier string raises ValueError."""
    with pytest.raises(ValueError, match="Unknown trust tier"):
        trust_tier_index("superuser")


# ---------------------------------------------------------------------------
# trust_tier_satisfies  (INV-TRUST-1)
# ---------------------------------------------------------------------------


def test_trust_tier_satisfies_same() -> None:
    """A tier satisfies itself."""
    assert trust_tier_satisfies("trusted", "trusted") is True


def test_trust_tier_satisfies_higher() -> None:
    """A higher tier satisfies a lower required tier."""
    assert trust_tier_satisfies("core", "trusted") is True


def test_trust_tier_satisfies_lower() -> None:
    """A lower tier does NOT satisfy a higher required tier."""
    assert trust_tier_satisfies("newcomer", "trusted") is False


def test_trust_tier_satisfies_admin_satisfies_all() -> None:
    """admin satisfies every required tier."""
    for tier in ("newcomer", "contributor", "trusted", "core", "admin"):
        assert trust_tier_satisfies("admin", tier) is True


def test_trust_tier_satisfies_newcomer_satisfies_only_newcomer() -> None:
    """newcomer satisfies only newcomer."""
    assert trust_tier_satisfies("newcomer", "newcomer") is True
    assert trust_tier_satisfies("newcomer", "contributor") is False


# ---------------------------------------------------------------------------
# worker_is_enrollable
# ---------------------------------------------------------------------------


def test_worker_is_enrollable_pending() -> None:
    """pending enrollment is enrollable."""
    assert worker_is_enrollable("pending") is True


def test_worker_is_enrollable_approved() -> None:
    """approved enrollment is not enrollable."""
    assert worker_is_enrollable("approved") is False


def test_worker_is_enrollable_suspended() -> None:
    """suspended enrollment is not enrollable."""
    assert worker_is_enrollable("suspended") is False


def test_worker_is_enrollable_revoked() -> None:
    """revoked enrollment is not enrollable."""
    assert worker_is_enrollable("revoked") is False


# ---------------------------------------------------------------------------
# worker_is_active
# ---------------------------------------------------------------------------


def test_worker_is_active_approved() -> None:
    """approved enrollment is active."""
    assert worker_is_active("approved") is True


def test_worker_is_active_pending() -> None:
    """pending enrollment is not active."""
    assert worker_is_active("pending") is False


def test_worker_is_active_suspended() -> None:
    """suspended enrollment is not active."""
    assert worker_is_active("suspended") is False


def test_worker_is_active_revoked() -> None:
    """revoked enrollment is not active."""
    assert worker_is_active("revoked") is False


# ---------------------------------------------------------------------------
# worker_can_promote  (INV-TRUST-2)
# ---------------------------------------------------------------------------


def test_worker_can_promote() -> None:
    """A non-admin tier with enough successful executions can promote."""
    assert worker_can_promote("newcomer", successful_executions=10, required_executions=10) is True
    assert (
        worker_can_promote("contributor", successful_executions=50, required_executions=20) is True
    )
    assert (
        worker_can_promote("trusted", successful_executions=100, required_executions=100) is True
    )
    assert worker_can_promote("core", successful_executions=200, required_executions=150) is True


def test_worker_can_promote_insufficient_executions() -> None:
    """A tier with too few successful executions cannot promote."""
    assert worker_can_promote("newcomer", successful_executions=5, required_executions=10) is False


def test_worker_can_promote_admin_blocked() -> None:
    """admin is the ceiling tier and can never promote, regardless of executions."""
    assert worker_can_promote("admin", successful_executions=9999, required_executions=1) is False


# ---------------------------------------------------------------------------
# worker_can_demote  (INV-TRUST-2)
# ---------------------------------------------------------------------------


def test_worker_can_demote() -> None:
    """A non-newcomer tier with enough failed executions can demote."""
    assert worker_can_demote("contributor", failed_executions=5, threshold=5) is True
    assert worker_can_demote("trusted", failed_executions=10, threshold=3) is True
    assert worker_can_demote("core", failed_executions=20, threshold=10) is True
    assert worker_can_demote("admin", failed_executions=100, threshold=50) is True


def test_worker_can_demote_insufficient_failures() -> None:
    """A tier below the failure threshold cannot demote."""
    assert worker_can_demote("trusted", failed_executions=2, threshold=5) is False


def test_worker_can_demote_newcomer_blocked() -> None:
    """newcomer is the floor tier and can never demote, regardless of failures."""
    assert worker_can_demote("newcomer", failed_executions=9999, threshold=1) is False


# ---------------------------------------------------------------------------
# credential_is_valid  (INV-WORKER-CRED-1)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
_FUTURE = _NOW + timedelta(hours=1)
_PAST = _NOW - timedelta(hours=1)


def test_credential_is_valid_active() -> None:
    """Un-revoked, un-expired credential is valid."""
    assert credential_is_valid(expires_at=_FUTURE, revoked_at=None, now=_NOW) is True


def test_credential_is_valid_expired() -> None:
    """Expired credential (even if not revoked) is invalid."""
    assert credential_is_valid(expires_at=_PAST, revoked_at=None, now=_NOW) is False


def test_credential_is_valid_revoked() -> None:
    """Revoked credential is invalid even when not yet expired."""
    assert (
        credential_is_valid(expires_at=_FUTURE, revoked_at=_PAST, now=_NOW) is False
    )


def test_credential_is_valid_revoked_and_expired() -> None:
    """Revoked and expired credential is invalid."""
    assert credential_is_valid(expires_at=_PAST, revoked_at=_PAST, now=_NOW) is False


def test_credential_is_valid_expires_exactly_now() -> None:
    """A credential that expires exactly at 'now' is NOT valid (boundary: >)."""
    assert credential_is_valid(expires_at=_NOW, revoked_at=None, now=_NOW) is False


# ---------------------------------------------------------------------------
# credential_is_expired
# ---------------------------------------------------------------------------


def test_credential_is_expired_past() -> None:
    """A credential with an expiry in the past is expired."""
    assert credential_is_expired(expires_at=_PAST, now=_NOW) is True


def test_credential_is_expired_future() -> None:
    """A credential with an expiry in the future is not expired."""
    assert credential_is_expired(expires_at=_FUTURE, now=_NOW) is False


def test_credential_is_expired_boundary() -> None:
    """expires_at == now counts as expired (boundary: <=)."""
    assert credential_is_expired(expires_at=_NOW, now=_NOW) is True
