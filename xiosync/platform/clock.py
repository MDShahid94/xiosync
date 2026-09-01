"""Injectable UTC clock (doc 04 §2.1 platform concerns).

All timestamps in XIOSYNC are timezone-aware UTC. Code under test receives a
``Clock`` so that time is controllable; production code uses ``SystemClock``.
Naive datetimes are a defect everywhere in the codebase.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """The single time source contract."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Production clock backed by the operating system."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Deterministic clock for tests; rejects naive datetimes."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant
