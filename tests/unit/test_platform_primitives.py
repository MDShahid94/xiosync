"""Unit tests for platform ids, clock, and crypto (M6, D-013)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from xiosync.platform.clock import FixedClock, SystemClock
from xiosync.platform.crypto import (
    constant_time_equals,
    hash_password,
    needs_rehash,
    verify_password,
)
from xiosync.platform.ids import UUID_VERSION, new_id

# --- ids (M6) ---------------------------------------------------------------


def test_new_id_is_stdlib_uuid_version_7() -> None:
    generated = new_id()
    assert type(generated) is uuid.UUID
    assert generated.version == UUID_VERSION == 7


def test_new_ids_are_unique_and_time_ordered() -> None:
    ids = [new_id() for _ in range(256)]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)  # UUIDv7 sorts by generation time


# --- clock ------------------------------------------------------------------


def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is UTC
    assert abs((now - datetime.now(UTC)).total_seconds()) < 5


def test_fixed_clock_is_deterministic_and_utc() -> None:
    instant = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    clock = FixedClock(instant)
    assert clock.now() == clock.now() == instant


def test_fixed_clock_normalizes_to_utc() -> None:
    from datetime import timezone

    offset = timezone(timedelta(hours=2))
    clock = FixedClock(datetime(2026, 7, 14, 14, 0, tzinfo=offset))
    assert clock.now() == datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def test_fixed_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 7, 14, 12, 0))


# --- crypto (M6) ------------------------------------------------------------


def test_password_roundtrip_and_mismatch() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("$argon2id$")
    assert verify_password(stored, "correct horse battery staple") is True
    assert verify_password(stored, "wrong password") is False


def test_verify_malformed_hash_returns_false_not_raises() -> None:
    assert verify_password("not-a-hash", "anything") is False
    assert verify_password("", "anything") is False


def test_empty_password_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        hash_password("")


def test_fresh_hash_needs_no_rehash() -> None:
    assert needs_rehash(hash_password("pw")) is False


def test_constant_time_equals() -> None:
    assert constant_time_equals(b"secret", b"secret") is True
    assert constant_time_equals(b"secret", b"Secret") is False
    assert constant_time_equals(b"", b"") is True
