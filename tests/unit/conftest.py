"""Unit-test fixtures for the XIOSYNC test suite.

Ensures the in-process ``event_registry`` contains every known event type
(core + XIOBR) before any test runs.  This prevents order-dependent failures
when a genesis-calling test populates the registry with only core types and a
subsequent XIOBR-service test then hits a populated-but-incomplete registry.

The ``event_registry`` singleton has ``_populated=False`` by default, which
makes ``is_valid()`` return ``True`` for everything (allow-all dev/test mode).
Once any call to ``register()`` flips ``_populated=True``, the registry
becomes authoritative and must contain all types that tests exercise.

Using a ``session``-scoped autouse fixture here means the registration happens
once per pytest session (cheap), and the registry is stable for the whole run.
"""

from __future__ import annotations

import pytest

from xiosync.domain.event_registry import event_registry
from xiosync.services.bootstrap import _CORE_EVENT_TYPES
from xiosync.services.xiobr_bootstrap import _EVENT_TYPES as _XIOBR_EVENT_TYPES


@pytest.fixture(autouse=True, scope="session")
def populate_event_registry() -> None:
    """Register all known event types into the in-process cache.

    Runs once per pytest session, before any test.  Safe to call even when
    the DB is not available — no I/O is performed.
    """
    all_event_types: list[str] = (
        [value for value, _ in _CORE_EVENT_TYPES]
        + [value for value, _ in _XIOBR_EVENT_TYPES]
    )
    event_registry.register(all_event_types)
