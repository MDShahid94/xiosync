"""Application-wide engine reference singleton (RULE-ARCH-1 safe).

Holds a single Engine reference set once during ASGI lifespan startup.
Read by subsystems that need DB access outside the HTTP request cycle
(e.g. XIOVIEW WebSocket handlers writing run-state transitions).

Usage::

    # app.py lifespan:
    from xiosync.platform.engine_ref import set_engine
    set_engine(engine)

    # Any async subsystem:
    from xiosync.platform.engine_ref import get_engine
    engine = get_engine()   # None if not yet initialized
"""
from __future__ import annotations
from typing import Any

__all__ = ["set_engine", "get_engine"]

_engine: Any = None  # type: Engine | None


def set_engine(engine: Any) -> None:
    """Register the application DB engine. Called once from app lifespan."""
    global _engine
    _engine = engine


def get_engine() -> Any:
    """Return the application DB engine, or None if startup hasn't run yet."""
    return _engine
