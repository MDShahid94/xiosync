"""Pure domain predicates for capabilities (Gap X-2).

This module is pure domain (RULE-ARCH-1): no I/O, no framework imports.
It governs execution modes, capability states, and version-aware matching.
"""

from __future__ import annotations

__all__ = [
    "CAPABILITY_STATES",
    "EXECUTION_MODES",
    "InvalidCapabilityStateError",
    "InvalidExecutionModeError",
    "capability_matches",
    "parse_versioned_capability",
    "validate_capability_state",
    "validate_execution_mode",
]

#: Supported execution modes for capabilities.
EXECUTION_MODES: frozenset[str] = frozenset({"sync", "async", "streaming"})

#: Capability lifecycle states.
CAPABILITY_STATES: frozenset[str] = frozenset({"draft", "active", "deprecated"})


class InvalidExecutionModeError(ValueError):
    """The execution_mode is not one of the declared modes."""

    def __init__(self, mode: str) -> None:
        super().__init__(
            f"execution_mode {mode!r} is not one of {sorted(EXECUTION_MODES)}"
        )
        self.mode = mode


class InvalidCapabilityStateError(ValueError):
    """The capability state is not one of the declared states."""

    def __init__(self, state: str) -> None:
        super().__init__(
            f"capability state {state!r} is not one of {sorted(CAPABILITY_STATES)}"
        )
        self.state = state


def validate_execution_mode(mode: str) -> str:
    """Return ``mode`` if it is valid, else raise."""
    if mode not in EXECUTION_MODES:
        raise InvalidExecutionModeError(mode)
    return mode


def validate_capability_state(state: str) -> str:
    """Return ``state`` if it is valid, else raise."""
    if state not in CAPABILITY_STATES:
        raise InvalidCapabilityStateError(state)
    return state


def parse_versioned_capability(capability: str) -> tuple[str, int | None]:
    """Parse a capability string into ``(name, version)``.

    Supports both exact (``"web-scraping"``) and versioned
    (``"web-scraping:v2"``) forms.  Returns ``(name, None)`` for unversioned.

    >>> parse_versioned_capability("web-scraping")
    ('web-scraping', None)
    >>> parse_versioned_capability("web-scraping:v2")
    ('web-scraping', 2)
    """
    if ":" not in capability:
        return capability, None
    name, version_str = capability.rsplit(":", 1)
    if version_str.startswith("v") and version_str[1:].isdigit():
        return name, int(version_str[1:])
    # Not a recognized version suffix — treat entire string as the name.
    return capability, None


def capability_matches(
    worker_manifest: list[str],
    required_capability: str,
    *,
    version_aware: bool = True,
) -> bool:
    """Check if a worker's capability manifest satisfies a required capability.

    When ``version_aware`` is True (configurable per-org):
    - ``"web-scraping:v2"`` in manifest satisfies ``"web-scraping:v1"``
      (higher version satisfies lower)
    - ``"web-scraping"`` (unversioned) satisfies any versioned requirement
    - Exact match always satisfies

    When ``version_aware`` is False, only exact string match is used.

    >>> capability_matches(["web-scraping:v2"], "web-scraping:v1")
    True
    >>> capability_matches(["web-scraping"], "web-scraping:v3")
    True
    >>> capability_matches(["screenshot:v1"], "web-scraping")
    False
    """
    if not version_aware:
        return required_capability in worker_manifest

    req_name, req_version = parse_versioned_capability(required_capability)

    for cap in worker_manifest:
        cap_name, cap_version = parse_versioned_capability(cap)
        if cap_name != req_name:
            continue
        # Name matches — check version compatibility.
        if req_version is None:
            # Unversioned requirement: any version of the same name satisfies.
            return True
        if cap_version is None:
            # Unversioned worker capability: satisfies any version requirement.
            return True
        if cap_version >= req_version:
            # Worker has equal or higher version.
            return True
    return False
