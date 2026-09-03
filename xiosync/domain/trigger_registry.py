"""Runtime trigger type registry — replaces hardcoded trigger_type CHECK constraints.

Populated at genesis/startup from the DB ``type_registry`` table
(category='trigger_type') and consulted by ``WorkflowTrigger`` service
instead of relying on the database-level CHECK constraint.

Design notes mirror ``event_registry``:
* Pure domain — no I/O, no ORM imports.
* ``_populated`` flag: allows all when not yet loaded (dev/test safety).
* Thread-safe via ``Lock``.
"""

from __future__ import annotations

from threading import Lock

__all__ = ["TriggerTypeRegistry", "trigger_registry"]

# Canonical seed values (also seeded into type_registry at genesis).
CRON = "cron"
EVENT = "event"
WEBHOOK = "webhook"

#: Closed set for fresh installs — mirrors what genesis seeds into type_registry.
_SEED_TRIGGER_TYPES: frozenset[str] = frozenset({CRON, EVENT, WEBHOOK})


class TriggerTypeRegistry:
    """Thread-safe in-process cache of valid trigger type strings.

    Populated at genesis/startup from the DB type_registry.
    Falls back to ALLOW-ALL if not yet populated (dev/test safety).
    """

    def __init__(self) -> None:
        self._types: frozenset[str] = frozenset()
        self._populated: bool = False
        self._lock: Lock = Lock()

    def register(self, trigger_types: list[str]) -> None:
        """Merge *trigger_types* into the cache and mark as populated.

        Idempotent — calling multiple times unions the sets.
        """
        with self._lock:
            self._types = self._types | frozenset(trigger_types)
            self._populated = True

    def is_valid(self, trigger_type: str) -> bool:
        """Return ``True`` if *trigger_type* is a registered type.

        Returns ``True`` unconditionally when the registry has not yet been
        populated — keeps tests that don't run genesis from failing.
        """
        if not self._populated:
            return True
        return trigger_type in self._types

    def all(self) -> frozenset[str]:
        """Return the current set of registered trigger types."""
        return self._types

    def reset(self) -> None:
        """Clear the registry — intended for test teardown only."""
        with self._lock:
            self._types = frozenset()
            self._populated = False


#: Module-level singleton — import and use this everywhere.
trigger_registry: TriggerTypeRegistry = TriggerTypeRegistry()
