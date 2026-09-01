"""Pure triggers domain (Gap R-5).

Trigger types, validation, and cron expression utilities. No I/O — RULE-ARCH-1.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

__all__ = [
    "InvalidCronExpressionError",
    "InvalidTriggerStateError",
    "InvalidTriggerTypeError",
    "TRIGGER_STATES",
    "TRIGGER_TYPES",
    "next_cron_fire",
    "validate_cron_expression",
    "validate_trigger_state",
    "validate_trigger_type",
]

TRIGGER_TYPES: frozenset[str] = frozenset({"cron", "event", "webhook"})
TRIGGER_STATES: frozenset[str] = frozenset({"active", "paused", "disabled"})

# Basic cron field ranges for validation.
_CRON_RANGES = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 7),    # day of week (0 and 7 = Sunday)
]


class InvalidTriggerTypeError(ValueError):
    """Raised when a trigger type is not recognized."""


class InvalidTriggerStateError(ValueError):
    """Raised when a trigger state is not valid."""


class InvalidCronExpressionError(ValueError):
    """Raised when a cron expression is malformed."""


def validate_trigger_type(trigger_type: str) -> None:
    """Reject unknown trigger types."""
    if trigger_type not in TRIGGER_TYPES:
        raise InvalidTriggerTypeError(
            f"unknown trigger type {trigger_type!r}; "
            f"expected one of {sorted(TRIGGER_TYPES)}"
        )


def validate_trigger_state(state: str) -> None:
    """Reject invalid trigger states."""
    if state not in TRIGGER_STATES:
        raise InvalidTriggerStateError(
            f"invalid trigger state {state!r}; "
            f"expected one of {sorted(TRIGGER_STATES)}"
        )


def validate_cron_expression(expression: str) -> None:
    """Validate a 5-field cron expression.

    Supports: ``*``, numeric values, ranges (``1-5``), steps (``*/5``),
    and comma-separated lists (``1,3,5``).
    """
    fields = expression.strip().split()
    if len(fields) != 5:
        raise InvalidCronExpressionError(
            f"cron expression must have exactly 5 fields, got {len(fields)}: {expression!r}"
        )

    for i, (field, (lo, hi)) in enumerate(zip(fields, _CRON_RANGES)):
        _validate_cron_field(field, lo, hi, i)


def _validate_cron_field(field: str, lo: int, hi: int, index: int) -> None:
    """Validate a single cron field."""
    field_names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    name = field_names[index]

    for part in field.split(","):
        part = part.strip()
        if not part:
            raise InvalidCronExpressionError(f"empty value in {name} field")

        # Handle step syntax: */5, 1-10/2
        step_parts = part.split("/")
        if len(step_parts) > 2:
            raise InvalidCronExpressionError(f"invalid step syntax in {name}: {part!r}")

        base = step_parts[0]
        if len(step_parts) == 2:
            step_val = step_parts[1]
            if not step_val.isdigit() or int(step_val) == 0:
                raise InvalidCronExpressionError(
                    f"step value must be a positive integer in {name}: {part!r}"
                )

        if base == "*":
            continue

        # Handle range: 1-5
        if "-" in base:
            range_parts = base.split("-")
            if len(range_parts) != 2:
                raise InvalidCronExpressionError(f"invalid range in {name}: {base!r}")
            for rp in range_parts:
                if not rp.isdigit():
                    raise InvalidCronExpressionError(
                        f"non-numeric range bound in {name}: {base!r}"
                    )
                val = int(rp)
                if val < lo or val > hi:
                    raise InvalidCronExpressionError(
                        f"{name} value {val} out of range [{lo}-{hi}]"
                    )
            continue

        # Plain numeric
        if not base.isdigit():
            raise InvalidCronExpressionError(
                f"non-numeric value in {name}: {base!r}"
            )
        val = int(base)
        if val < lo or val > hi:
            raise InvalidCronExpressionError(
                f"{name} value {val} out of range [{lo}-{hi}]"
            )


def next_cron_fire(expression: str, after: datetime) -> datetime | None:
    """Compute the next fire time for a cron expression after ``after``.

    This is a simplified minute-resolution implementation suitable for
    the platform's scheduling needs. Returns ``None`` if no valid fire time
    can be found within 366 days (safety bound).

    For production deployments, orgs may plug in a full-featured cron library
    (e.g. croniter) via the configurable trigger evaluation layer.
    """
    fields = expression.strip().split()
    if len(fields) != 5:
        return None

    minutes = _expand_field(fields[0], 0, 59)
    hours = _expand_field(fields[1], 0, 23)
    days = _expand_field(fields[2], 1, 31)
    months = _expand_field(fields[3], 1, 12)
    dows = _expand_field(fields[4], 0, 7)
    # Normalize Sunday: 7 → 0
    dows = {0 if d == 7 else d for d in dows}

    if not all([minutes, hours, days, months, dows]):
        return None

    from datetime import timedelta

    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366)

    while candidate < limit:
        if (
            candidate.month in months
            and candidate.day in days
            and candidate.weekday() in _iso_to_cron_dow(dows)
            and candidate.hour in hours
            and candidate.minute in minutes
        ):
            return candidate
        candidate += timedelta(minutes=1)

    return None


def _expand_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand a cron field into a set of matching values."""
    result: set[int] = set()
    for part in field.split(","):
        step_parts = part.split("/")
        base = step_parts[0]
        step = int(step_parts[1]) if len(step_parts) == 2 else 1

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            rp = base.split("-")
            start, end = int(rp[0]), int(rp[1])
        else:
            start = end = int(base)

        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                result.add(v)
    return result


def _iso_to_cron_dow(cron_dows: set[int]) -> set[int]:
    """Convert cron day-of-week (0=Sun) to Python weekday (0=Mon)."""
    mapping = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    return {mapping[d] for d in cron_dows if d in mapping}
