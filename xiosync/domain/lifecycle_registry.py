"""Runtime lifecycle state registry — replaces hardcoded state CHECK constraints.

Populated at genesis/startup from the DB ``type_registry`` table
(category='lifecycle_state') and consulted by service-layer state
machine transitions instead of database-level CHECK constraints.

Design notes mirror ``event_registry``:
* Pure domain — no I/O, no ORM imports.
* ``_populated`` flag: allows all when not yet loaded (dev/test safety).
* Thread-safe via ``Lock``.
"""

from __future__ import annotations

from threading import Lock

__all__ = ["LifecycleStateRegistry", "lifecycle_registry"]

# Canonical seed values — mirror _CORE_LIFECYCLE_STATES in bootstrap.py.
PROPOSED = "proposed"
DESIGNING = "designing"
IMPLEMENTING = "implementing"
VALIDATING = "validating"
INITIALIZING = "initializing"
ACTIVE = "active"
UPDATING = "updating"
SUSPENDED = "suspended"
MIGRATING = "migrating"
TERMINATING = "terminating"
TERMINATED = "terminated"
ARCHIVED = "archived"

# Extended states used by XIOGRID subsystem (Gap 1.2 — previously hardcoded in CHECK constraints).
FAILED = "failed"
READY = "ready"
BUSY = "busy"
ERROR = "error"
CONFIGURING = "configuring"
DRAFT = "draft"
DEPRECATED = "deprecated"
PENDING_APPROVAL = "pending_approval"
APPROVED = "approved"
REVOKED = "revoked"
DISABLED = "disabled"
PAUSED = "paused"

#: Full seed set — mirrors what genesis + XIOGRID bootstrap seed into type_registry.
_SEED_LIFECYCLE_STATES: frozenset[str] = frozenset({
    PROPOSED, DESIGNING, IMPLEMENTING, VALIDATING,
    INITIALIZING, ACTIVE, UPDATING, SUSPENDED,
    MIGRATING, TERMINATING, TERMINATED, ARCHIVED,
    # Extended states:
    FAILED, READY, BUSY, ERROR, CONFIGURING,
    DRAFT, DEPRECATED, PENDING_APPROVAL, APPROVED, REVOKED,
    DISABLED, PAUSED,
})


class LifecycleStateRegistry:
    """Thread-safe in-process cache of valid lifecycle state strings.

    Populated at genesis/startup from the DB type_registry.
    Falls back to ALLOW-ALL if not yet populated (dev/test safety).
    """

    def __init__(self) -> None:
        self._states: frozenset[str] = frozenset()
        self._populated: bool = False
        self._lock: Lock = Lock()

    def register(self, states: list[str]) -> None:
        """Merge *states* into the cache and mark as populated. Idempotent."""
        with self._lock:
            self._states = self._states | frozenset(states)
            self._populated = True

    def is_valid(self, state: str) -> bool:
        """Return ``True`` if *state* is a registered lifecycle state.

        Returns ``True`` unconditionally when the registry has not yet been
        populated — keeps tests that don't run genesis from failing.
        """
        if not self._populated:
            return True
        return state in self._states

    def all(self) -> frozenset[str]:
        """Return the current set of registered lifecycle states."""
        return self._states

    def reset(self) -> None:
        """Clear the registry — intended for test teardown only."""
        with self._lock:
            self._states = frozenset()
            self._populated = False


#: Module-level singleton — import and use this everywhere.
lifecycle_registry: LifecycleStateRegistry = LifecycleStateRegistry()
