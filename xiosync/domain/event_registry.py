"""Runtime event type registry — replaces the hardcoded EVENT_TYPES frozenset.

This module provides a thread-safe, in-process cache of valid event type
strings. It is populated at genesis/startup from the DB ``type_registry``
table (category='event_type') and consulted by ``EventService`` instead of
the old static frozenset.

Design notes:
* Pure domain — no I/O, no ORM imports. Population is the caller's concern
  (``BootstrapService``, ``xiogrid_bootstrap``).
* ``_populated`` flag: if the registry has never been loaded (e.g. unit
  tests that don't run genesis) every type is treated as valid, preserving
  test ergonomics without any extra setup.
* Thread-safe: ``register()`` holds a lock so concurrent startup paths
  (unlikely but possible) cannot corrupt the frozenset.
"""

from __future__ import annotations

from threading import Lock

__all__ = ["EventTypeRegistry", "event_registry"]


class EventTypeRegistry:
    """Thread-safe in-process cache of valid event type strings.

    Populated at genesis/startup from the DB type_registry.
    Falls back to ALLOW-ALL if not yet populated (dev/test safety).
    """

    def __init__(self) -> None:
        self._types: frozenset[str] = frozenset()
        self._populated: bool = False
        self._lock: Lock = Lock()

    def register(self, event_types: list[str]) -> None:
        """Merge *event_types* into the cache and mark as populated.

        Idempotent — calling multiple times (e.g. core + xiogrid bootstrap)
        unions the sets rather than replacing them.
        """
        with self._lock:
            self._types = self._types | frozenset(event_types)
            self._populated = True

    def is_valid(self, event_type: str) -> bool:
        """Return ``True`` if *event_type* is a registered type.

        Returns ``True`` unconditionally when the registry has not yet been
        populated — this keeps tests that don't run genesis from failing on
        type validation.
        """
        if not self._populated:
            return True  # dev/test safety: allow all if registry not loaded
        return event_type in self._types

    def all(self) -> frozenset[str]:
        """Return the current set of registered event types."""
        return self._types

    def reset(self) -> None:
        """Clear the registry — intended for test teardown only."""
        with self._lock:
            self._types = frozenset()
            self._populated = False


#: Module-level singleton — import and use this everywhere.
event_registry: EventTypeRegistry = EventTypeRegistry()
