"""Password hashing and constant-time comparison (M6, doc 05, D-013).

``cryptography`` and ``argon2-cffi`` are hard dependencies: this module fails
at import time if either is absent, which fails startup (INV-STARTUP-1).
There is no plaintext or weaker fallback path, ever (M6).
"""

from __future__ import annotations

import hmac

# Hard-dependency probe (M6): importing cryptography proves the compiled
# backend is present; startup dies here if it is not.
import cryptography  # noqa: F401
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash ``password`` with Argon2id using library-recommended parameters."""
    if not password:
        raise ValueError("password must not be empty")
    return _hasher.hash(password)


def verify_password(stored_hash: str, candidate: str) -> bool:
    """Return True only if ``candidate`` matches ``stored_hash``.

    Malformed hashes and mismatches both return False — callers never branch
    on *why* verification failed (no oracle).
    """
    try:
        return _hasher.verify(stored_hash, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Return True if ``stored_hash`` predates current Argon2 parameters."""
    return _hasher.check_needs_rehash(stored_hash)


def constant_time_equals(left: bytes, right: bytes) -> bool:
    """Timing-safe equality for secret material (tokens, signatures)."""
    return hmac.compare_digest(left, right)
